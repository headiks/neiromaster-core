"""
Единое хранилище пользователей: аккаунт + роль + профиль адаптации.

Одна запись описывает и вход в систему, и то, что нужно для адаптации:

    аккаунт   — username, scrypt-хэш пароля, роль, активность
    профиль   — ФИО, должность, подразделение, контакт
    адаптация — наставник, руководитель, назначенный план, дата выхода, статус

Роли:
    owner    — главный администратор. Один. Может всё: удалять пользователей,
               назначать и снимать администраторов, передавать роль главного.
    admin    — администратор. База знаний, конструктор планов, заведение сотрудников
               и назначение им планов. Не может трогать других администраторов,
               удалять пользователей и раздавать права.
    employee — сотрудник. Чат с ассистентом и своё расписание адаптации.

Хранилище: таблица users в SQLite (см. db.py). Открытых паролей нет — только соль
и scrypt-хэш. Со старого data/users.json данные переносятся автоматически при старте.
"""

import os
import json
import time
import hmac
import uuid
import secrets
import hashlib
import threading
from pathlib import Path
from typing import Optional

import db

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USERS_PATH = DATA_DIR / "users.json"          # старое хранилище — источник разовой миграции
LEGACY_EMPLOYEES_PATH = DATA_DIR / "employees.json"
INITIAL_CREDENTIALS_PATH = DATA_DIR / "owner_initial_credentials.txt"

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_EMPLOYEE = "employee"
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_EMPLOYEE)
ADMIN_ROLES = (ROLE_OWNER, ROLE_ADMIN)

ADAPTATION_STATUSES = {"planned", "active", "done", "paused"}

MIN_PASSWORD_LENGTH = 8
MIN_USERNAME_LENGTH = 3
USERNAME_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._-")

# Самостоятельно зарегистрировавшийся сотрудник ждёт подтверждения администратора.
# Поставьте False, если сервер стоит в закрытом контуре и модерация не нужна.
REQUIRE_REGISTRATION_APPROVAL = True

# Параметры scrypt (RFC 7914): подбор пароля дорогой, проверка при входе — миллисекунды
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

_lock = threading.RLock()

# Порядок колонок таблицы users (см. db.SCHEMA). Совпадает с ключами _blank_user.
_COLUMNS = (
    "id", "username", "full_name", "role", "active", "salt", "hash",
    "must_change_credentials", "created_at", "updated_at", "password_changed_at",
    "position", "department", "contact", "mentor", "manager", "plan_id",
    "start_date", "status", "notes", "created_by",
)
_BOOL_COLUMNS = ("active", "must_change_credentials")


# ---------- Пароли ----------
def hash_password(password: str, salt: Optional[bytes] = None) -> tuple:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: Optional[str], hash_hex: Optional[str]) -> bool:
    if not salt_hex or not hash_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    _, digest = hash_password(password, salt)
    return hmac.compare_digest(digest, hash_hex)


def validate_password(password: str):
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")


def normalize_username(username: str) -> str:
    """Логины приводим к нижнему регистру: «Ivanov» и «ivanov» — один человек."""
    username = (username or "").strip().lower()
    if len(username) < MIN_USERNAME_LENGTH:
        raise ValueError(f"Логин должен быть не короче {MIN_USERNAME_LENGTH} символов")
    if not set(username) <= USERNAME_ALLOWED:
        raise ValueError("Логин может содержать только латинские буквы, цифры, точку, дефис и подчёркивание")
    return username


# ---------- Шифрование ПДн в БД (at rest) ----------
# Свободный текст профиля (ФИО, должность, контакты, наставник, руководитель,
# заметки) шифруется в БД, ЕСЛИ задан ключ NEIROMASTER_PII_KEY (Fernet-ключ,
# сгенерировать: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
# Без ключа поведение прежнее — открытый текст. Шифртекст помечается префиксом
# «enc:», поэтому старые открытые строки и новые зашифрованные уживаются в одной
# таблице без миграции: расшифровка трогает только значения с префиксом.
#
# Защищает утёкший дамп/бэкап БД (pg_dump, украденный том). Ключ лежит в
# .env.production рядом с БД, поэтому от полной компрометации хоста не спасает —
# для этого нужно ещё шифрование диска (LUKS). Логин/поиск не затрагиваются:
# username/role/id остаются открытыми, сортировка по ФИО идёт уже по расшифрованным
# значениям в Python (list_users), не в SQL.
_ENCRYPTED_COLUMNS = ("full_name", "position", "department", "contact", "mentor", "manager", "notes")
_PII_PREFIX = "enc:"


def _fernet():
    key = os.environ.get("NEIROMASTER_PII_KEY")
    if not key:
        return None                       # ключ не задан — шифрование выключено
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def _encrypt_field(value):
    f = _fernet()
    if f is None or not value:
        return value
    return _PII_PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt_field(value):
    if not isinstance(value, str) or not value.startswith(_PII_PREFIX):
        return value                      # старое поле открытым текстом — вернуть как есть
    f = _fernet()
    if f is None:
        return value                      # ключ убрали — не падать, отдать хотя бы шифртекст
    from cryptography.fernet import InvalidToken
    try:
        return f.decrypt(value[len(_PII_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        return value


# ---------- Хранилище (PostgreSQL) ----------
def _row_to_user(row) -> dict:
    user = {col: row[col] for col in _COLUMNS}
    for col in _BOOL_COLUMNS:
        user[col] = bool(user[col])
    for col in _ENCRYPTED_COLUMNS:
        user[col] = _decrypt_field(user[col])
    return user


def _insert(user: dict):
    values = [_encrypt_field(user[c]) if c in _ENCRYPTED_COLUMNS else user[c] for c in _COLUMNS]
    placeholders = ", ".join("%s" for _ in _COLUMNS)
    db.execute(f"INSERT INTO users ({', '.join(_COLUMNS)}) VALUES ({placeholders})", values)


def _save_user(user: dict):
    """Перезапись всех колонок записи по id (аналог прежнего load->mutate->save)."""
    cols = [c for c in _COLUMNS if c != "id"]
    values = [_encrypt_field(user.get(c)) if c in _ENCRYPTED_COLUMNS else user.get(c) for c in cols]
    values.append(user["id"])
    db.execute(f"UPDATE users SET {', '.join(c + ' = %s' for c in cols)} WHERE id = %s", values)


def public_view(user: dict) -> dict:
    """Запись пользователя без секретов — всё, что можно отдать в API."""
    return {k: v for k, v in user.items() if k not in ("salt", "hash")}


def _blank_user(**fields) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    user = {
        "id": str(uuid.uuid4()),
        "username": None,
        "full_name": "",
        "role": ROLE_EMPLOYEE,
        "active": True,
        "salt": None,
        "hash": None,
        "must_change_credentials": False,
        "created_at": now,
        "updated_at": now,
        "password_changed_at": None,
        # Профиль и адаптация
        "position": "",
        "department": "",
        "contact": "",
        "mentor": "",
        "manager": "",
        "plan_id": None,
        "start_date": None,
        "status": "planned",
        "notes": "",
        "created_by": None,
    }
    user.update(fields)
    return user


# ---------- Чтение ----------
def list_users() -> list:
    rows = db.query("SELECT * FROM users", (), "all")
    users = [_row_to_user(r) for r in rows]
    return sorted((public_view(u) for u in users),
                  key=lambda u: (u["role"] != ROLE_OWNER, u["role"] != ROLE_ADMIN,
                                 u.get("full_name") or ""))


def get_user(user_id: str) -> Optional[dict]:
    row = db.query("SELECT * FROM users WHERE id = %s", (user_id,), "one")
    return _row_to_user(row) if row else None


def get_by_username(username: str) -> Optional[dict]:
    try:
        username = normalize_username(username)
    except ValueError:
        return None
    row = db.query("SELECT * FROM users WHERE username = %s", (username,), "one")
    return _row_to_user(row) if row else None


def get_owner() -> Optional[dict]:
    row = db.query("SELECT * FROM users WHERE role = %s LIMIT 1", (ROLE_OWNER,), "one")
    return _row_to_user(row) if row else None


def count_users() -> int:
    return db.query("SELECT COUNT(*) AS n FROM users", (), "one")["n"]


def _username_taken(username: str, exclude_id: Optional[str] = None) -> bool:
    row = db.query("SELECT id FROM users WHERE username = %s", (username,), "one")
    return bool(row) and row["id"] != exclude_id


# ---------- Права ----------
def is_admin(user: dict) -> bool:
    return bool(user) and user.get("role") in ADMIN_ROLES


def is_owner(user: dict) -> bool:
    return bool(user) and user.get("role") == ROLE_OWNER


def can_manage(actor: dict, target: dict) -> bool:
    """
    Кого актор вправе редактировать.
    Главный администратор — всех. Обычный администратор — только сотрудников:
    иначе он мог бы сбросить пароль другому администратору и обойти ограничения.
    """
    if not is_admin(actor) or not target:
        return False
    if is_owner(actor):
        return True
    return target.get("role") == ROLE_EMPLOYEE


def ensure_can_manage(actor: dict, target: dict):
    if not can_manage(actor, target):
        raise PermissionError("Недостаточно прав: изменять администраторов может только главный администратор")


# ---------- Запись ----------
def _apply_profile(user: dict, raw: dict) -> dict:
    """Профильные и адаптационные поля. Роль, логин и пароль сюда не входят."""
    status = raw.get("status") if raw.get("status") in ADAPTATION_STATUSES else user.get("status", "planned")
    user.update({
        "full_name": (raw.get("full_name") or user.get("full_name") or "").strip() or "Без имени",
        "position": (raw.get("position") or "").strip(),
        "department": (raw.get("department") or "").strip(),
        "contact": (raw.get("contact") or "").strip(),
        "mentor": (raw.get("mentor") or "").strip(),
        "manager": (raw.get("manager") or "").strip(),
        "plan_id": (raw.get("plan_id") or "").strip() or None,
        "start_date": str(raw.get("start_date") or "")[:10] or None,
        "status": status,
        "notes": (raw.get("notes") or "").strip(),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return user


def create_user(raw: dict, actor: Optional[dict] = None, role: str = ROLE_EMPLOYEE,
                active: bool = True, must_change_credentials: bool = False) -> dict:
    """
    Создаёт пользователя. Логин и пароль необязательны: администратор может завести
    профиль заранее, а логин выдать позже — войти без пароля всё равно нельзя.
    """
    username = raw.get("username")
    password = raw.get("password")

    with _lock:
        if username:
            username = normalize_username(username)
            if _username_taken(username):
                raise ValueError(f"Логин «{username}» уже занят")
        if password:
            validate_password(password)

        user = _blank_user(role=role if role in ROLES else ROLE_EMPLOYEE,
                           active=active,
                           username=username or None,
                           created_by=(actor or {}).get("id"))
        _apply_profile(user, raw)
        if password:
            user["salt"], user["hash"] = hash_password(password)
            user["password_changed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            user["must_change_credentials"] = must_change_credentials

        _insert(user)
    return dict(user)


def update_profile(user_id: str, raw: dict) -> Optional[dict]:
    with _lock:
        user = get_user(user_id)
        if not user:
            return None
        _apply_profile(user, raw)
        _save_user(user)
    return dict(user)


def set_username(user_id: str, username: str) -> dict:
    username = normalize_username(username)
    with _lock:
        user = get_user(user_id)
        if not user:
            raise ValueError("Пользователь не найден")
        if _username_taken(username, exclude_id=user_id):
            raise ValueError(f"Логин «{username}» уже занят")
        user["username"] = username
        user["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_user(user)
    return dict(user)


def set_password(user_id: str, password: str, must_change: bool = False) -> dict:
    validate_password(password)
    salt_hex, hash_hex = hash_password(password)
    with _lock:
        user = get_user(user_id)
        if not user:
            raise ValueError("Пользователь не найден")
        user["salt"] = salt_hex
        user["hash"] = hash_hex
        user["password_changed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        user["must_change_credentials"] = must_change
        user["updated_at"] = user["password_changed_at"]
        _save_user(user)
    return dict(user)


def set_credentials(user_id: str, username: str, password: str) -> dict:
    """
    Одновременная смена логина и пароля — первичная настройка главного администратора
    после входа по сгенерированным данным.
    """
    username = normalize_username(username)
    validate_password(password)
    salt_hex, hash_hex = hash_password(password)
    with _lock:
        user = get_user(user_id)
        if not user:
            raise ValueError("Пользователь не найден")
        if _username_taken(username, exclude_id=user_id):
            raise ValueError(f"Логин «{username}» уже занят")
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        user.update({"username": username, "salt": salt_hex, "hash": hash_hex,
                     "must_change_credentials": False,
                     "password_changed_at": now, "updated_at": now})
        _save_user(user)
        # Первичная подсказка с логином и паролем больше не нужна
        INITIAL_CREDENTIALS_PATH.unlink(missing_ok=True)
    return dict(user)


def set_active(user_id: str, active: bool) -> Optional[dict]:
    with _lock:
        user = get_user(user_id)
        if not user:
            return None
        if user["role"] == ROLE_OWNER and not active:
            raise ValueError("Нельзя деактивировать главного администратора")
        user["active"] = bool(active)
        user["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_user(user)
    return dict(user)


def set_role(user_id: str, role: str) -> dict:
    """Назначение и снятие прав администратора. Роль owner так не выдаётся."""
    if role not in (ROLE_ADMIN, ROLE_EMPLOYEE):
        raise ValueError("Роль может быть только «администратор» или «сотрудник»")
    with _lock:
        user = get_user(user_id)
        if not user:
            raise ValueError("Пользователь не найден")
        if user["role"] == ROLE_OWNER:
            raise ValueError("Сначала передайте права главного администратора другому человеку")
        if role == ROLE_ADMIN and not user.get("hash"):
            raise ValueError("У пользователя нет пароля — сначала выдайте ему логин и пароль")
        user["role"] = role
        user["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_user(user)
    return dict(user)


def transfer_ownership(current_owner_id: str, new_owner_id: str) -> dict:
    """Передача роли главного администратора. Прежний владелец становится админом."""
    with _lock:
        current = get_user(current_owner_id)
        new_owner = get_user(new_owner_id)
        if not current or current["role"] != ROLE_OWNER:
            raise ValueError("Передать права может только действующий главный администратор")
        if not new_owner:
            raise ValueError("Пользователь не найден")
        if new_owner["id"] == current["id"]:
            raise ValueError("Это и есть текущий главный администратор")
        if not new_owner.get("hash"):
            raise ValueError("У пользователя нет пароля — сначала выдайте ему логин и пароль")
        if not new_owner.get("active"):
            raise ValueError("Нельзя передать права неактивному пользователю")

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        new_owner["role"] = ROLE_OWNER
        new_owner["updated_at"] = now
        current["role"] = ROLE_ADMIN
        current["updated_at"] = now
        _save_user(new_owner)
        _save_user(current)
    return dict(new_owner)


def delete_user(user_id: str) -> bool:
    with _lock:
        user = get_user(user_id)
        if not user:
            return False
        if user["role"] == ROLE_OWNER:
            raise ValueError("Нельзя удалить главного администратора — сначала передайте права")
        db.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return True


# ---------- Регистрация сотрудника ----------
def register_employee(username: str, password: str, full_name: str,
                      position: str = "", contact: str = "") -> dict:
    """
    Самостоятельная регистрация сотрудника. По умолчанию аккаунт создаётся
    неактивным: пока администратор его не подтвердит, войти нельзя — иначе
    доступ к внутренним регламентам получил бы любой, кто открыл страницу.
    """
    return create_user(
        {"username": username, "password": password, "full_name": full_name,
         "position": position, "contact": contact},
        role=ROLE_EMPLOYEE,
        active=not REQUIRE_REGISTRATION_APPROVAL,
    )


# ---------- Первый запуск ----------
def ensure_owner() -> Optional[dict]:
    """
    Создаёт главного администратора при первом запуске со сгенерированными
    логином и паролем. Возвращает их один раз — чтобы напечатать в консоли сервера.
    """
    with _lock:
        if count_users() > 0:
            return None

        username = f"admin-{secrets.token_hex(3)}"
        password = secrets.token_urlsafe(12)
        create_user(
            {"username": username, "password": password, "full_name": "Главный администратор"},
            role=ROLE_OWNER,
            active=True,
            must_change_credentials=True,
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INITIAL_CREDENTIALS_PATH.write_text(
        f"Логин: {username}\nПароль: {password}\n\n"
        "Одноразовые данные для первого входа. После входа система попросит задать\n"
        "свой логин и пароль, и этот файл будет удалён автоматически.\n",
        encoding="utf-8",
    )
    try:
        os.chmod(INITIAL_CREDENTIALS_PATH, 0o600)
    except OSError:
        pass  # на Windows права не выставить — не критично
    return {"username": username, "password": password}


# ---------- Миграции со старых форматов ----------
def _import_records(records) -> int:
    """Вставляет записи пользователей, пропуская те, чей id уже есть в БД."""
    existing = {r["id"] for r in db.query("SELECT id FROM users", (), "all")}
    moved = 0
    for rec in records:
        uid = rec.get("id") or str(uuid.uuid4())
        if uid in existing:
            continue
        user = _blank_user()
        # Переносим все известные колонки как есть (включая salt/hash/role), профиль — нормализуем
        for col in _COLUMNS:
            if col in rec and rec[col] is not None:
                user[col] = rec[col]
        user["id"] = uid
        user["active"] = bool(rec.get("active", user["active"]))
        user["must_change_credentials"] = bool(rec.get("must_change_credentials", False))
        _insert(user)
        existing.add(uid)
        moved += 1
    return moved


def migrate_legacy_json_users() -> int:
    """Перенос аккаунтов из старого data/users.json в SQLite (разово)."""
    if not USERS_PATH.exists():
        return 0
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0
    with _lock:
        moved = _import_records(list(data.values()))
    USERS_PATH.replace(USERS_PATH.with_suffix(".json.migrated"))
    return moved


def migrate_legacy_employees() -> int:
    """
    Переносит записи из старого data/employees.json (сотрудники без аккаунтов)
    в общее хранилище. Логин и пароль администратор выдаёт им уже в админке.
    """
    if not LEGACY_EMPLOYEES_PATH.exists():
        return 0
    try:
        with open(LEGACY_EMPLOYEES_PATH, "r", encoding="utf-8") as f:
            legacy = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0
    if not legacy:
        LEGACY_EMPLOYEES_PATH.unlink(missing_ok=True)
        return 0

    with _lock:
        existing = {r["id"] for r in db.query("SELECT id FROM users", (), "all")}
        moved = 0
        for employee in legacy.values():
            if employee.get("id") in existing:
                continue
            user = _blank_user(id=employee.get("id") or str(uuid.uuid4()), role=ROLE_EMPLOYEE)
            _apply_profile(user, employee)
            user["created_at"] = employee.get("created_at") or user["created_at"]
            _insert(user)
            moved += 1

    LEGACY_EMPLOYEES_PATH.replace(LEGACY_EMPLOYEES_PATH.with_suffix(".json.migrated"))
    return moved


if __name__ == "__main__":
    # Проверка шифрования ПДн без БД: round-trip, префикс, обратная совместимость.
    from cryptography.fernet import Fernet as _F
    os.environ["NEIROMASTER_PII_KEY"] = _F.generate_key().decode()
    _ct = _encrypt_field("Иванов Иван")
    assert _ct.startswith(_PII_PREFIX) and _ct != "Иванов Иван"
    assert _decrypt_field(_ct) == "Иванов Иван"
    assert _decrypt_field("открытый текст") == "открытый текст"   # старое поле не трогаем
    assert _encrypt_field("") == "" and _decrypt_field("") == ""  # пустое не шифруем
    os.environ.pop("NEIROMASTER_PII_KEY")
    assert _decrypt_field(_ct) == _ct        # без ключа не падаем, отдаём шифртекст
    print("OK: шифрование ПДн — round-trip, префикс, совместимость со старыми строками")
