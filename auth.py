"""
Вход в систему и сессии — для всех пользователей: и сотрудников, и администраторов.

Учётные записи, пароли и роли лежат в users.py. Здесь только:
    - проверка логина и пароля;
    - защита от перебора;
    - выдача и проверка токена сессии.

Сессии хранятся в PostgreSQL (таблица sessions, см. db.py): переживают перезапуск
сервера (пользователей не разлогинивает) и общие для всех uvicorn-воркеров.
Защита от перебора (лок-аут) остаётся в памяти процесса — она короткоживущая и
сбрасывается при рестарте, это допустимо.
"""

import os
import time
import secrets
import threading
from typing import Optional

import db
import users

COOKIE_NAME = "nm_session"
SESSION_TTL = 12 * 3600          # сколько живёт сессия без активности

# Флаг Secure у cookie сессии: по умолчанию ВКЛ (токен не уходит по чистому HTTP).
# Для локальной разработки без TLS выключается NEIROMASTER_INSECURE_COOKIE=1.
COOKIE_SECURE = os.environ.get("NEIROMASTER_INSECURE_COOKIE", "").lower() not in ("1", "true", "yes")

# Защита от перебора: после LOCKOUT_ATTEMPTS неудач вход блокируется на LOCKOUT_SECONDS.
# Ключ — САМ логин, а не (IP, логин): иначе распределённый перебор с ротацией IP
# получал бы свежий лимит на каждый адрес и обходил защиту полностью.
# ponytail: обратная сторона — таргетированный лок-аут (10 неудач по чужому логину
# блокируют его вход на 5 мин). Для внутреннего инструмента приемлемо; при росту
# поверхности — капча/экспоненциальная задержка вместо жёсткой блокировки.
LOCKOUT_ATTEMPTS = 10
LOCKOUT_SECONDS = 300

_lock = threading.Lock()
_failures: dict = {}


# ---------- Блокировка перебора ----------
def _is_locked(key: str) -> int:
    """Сколько секунд осталось до разблокировки (0 — не заблокирован)."""
    record = _failures.get(key)
    if not record or record["count"] < LOCKOUT_ATTEMPTS:
        return 0
    remaining = int(record["last"] + LOCKOUT_SECONDS - time.time())
    if remaining <= 0:
        _failures.pop(key, None)
        return 0
    return remaining


def _record_failure(key: str):
    record = _failures.setdefault(key, {"count": 0, "last": 0})
    # Серия считается прерванной, если между попытками прошло больше окна блокировки
    if time.time() - record["last"] > LOCKOUT_SECONDS:
        record["count"] = 0
    record["count"] += 1
    record["last"] = time.time()


# ---------- Вход ----------
def login(username: str, password: str, client: str = "") -> tuple:
    """
    Возвращает (токен сессии, пользователь) либо поднимает ValueError с причиной отказа.
    """
    username = (username or "").strip().lower()
    key = username    # лок-аут по аккаунту, независимо от IP (см. LOCKOUT_ATTEMPTS)

    with _lock:
        locked_for = _is_locked(key)
    if locked_for:
        raise ValueError(f"Слишком много неудачных попыток. Повторите через {locked_for} сек.")

    user = users.get_by_username(username)

    # Хэш считаем всегда — иначе по времени ответа видно, существует ли логин
    salt = user["salt"] if user and user.get("salt") else secrets.token_bytes(16).hex()
    stored = user["hash"] if user and user.get("hash") else secrets.token_bytes(32).hex()
    password_ok = users.verify_password(password or "", salt, stored)

    if not user or not user.get("hash") or not password_ok:
        with _lock:
            _record_failure(key)
        raise ValueError("Неверный логин или пароль")

    if not user.get("active"):
        raise ValueError("Учётная запись ещё не подтверждена администратором")

    with _lock:
        _failures.pop(key, None)

    now = time.time()
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions (token, user_id, created_at, seen_at) VALUES (%s, %s, %s, %s)",
               (token, user["id"], now, now))
    db.execute("DELETE FROM sessions WHERE seen_at < %s", (now - SESSION_TTL,))  # чистим протухшие
    return token, user


def get_session_user(token: Optional[str]) -> Optional[dict]:
    """
    Возвращает актуальную запись пользователя по токену и продлевает сессию.
    Роль и активность читаются из хранилища при каждом запросе — снятие прав
    или блокировка действуют сразу, без ожидания перелогина.
    """
    if not token:
        return None
    now = time.time()
    session = db.query("SELECT user_id, seen_at FROM sessions WHERE token = %s", (token,), "one")
    if not session:
        return None
    if now - session["seen_at"] > SESSION_TTL:
        db.execute("DELETE FROM sessions WHERE token = %s", (token,))
        return None
    db.execute("UPDATE sessions SET seen_at = %s WHERE token = %s", (now, token))

    user = users.get_user(session["user_id"])
    if not user or not user.get("active"):
        db.execute("DELETE FROM sessions WHERE token = %s", (token,))
        return None
    return user


def logout(token: Optional[str]):
    if not token:
        return
    db.execute("DELETE FROM sessions WHERE token = %s", (token,))


def drop_user_sessions(user_id: str):
    """Разлогинить пользователя везде — после смены пароля или снятия прав."""
    db.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


def change_own_password(user: dict, old_password: str, new_password: str):
    if not users.verify_password(old_password or "", user.get("salt"), user.get("hash")):
        raise ValueError("Текущий пароль указан неверно")
    users.set_password(user["id"], new_password)
    drop_user_sessions(user["id"])
