"""
Тестовые роли и тестовые документы — проверка разграничения прав и раскладки хранилища.

Что делает (идемпотентно, повторный запуск не плодит дублей):
  1) находит суперадмина (owner) — он создаётся при первом старте приложения;
  2) заводит двух администраторов в РАЗНЫХ отделах и трёх сотрудников;
  3) загружает от имени каждого администратора по два коротких документа;
  4) печатает получившееся дерево хранилища и таблицу «кто что видит».

Ожидаемая раскладка оригиналов в S3:

    documents/<суперадмин>/<администратор-1>/<файлы>
    documents/<суперадмин>/<администратор-2>/<файлы>
    documents/<суперадмин>/<суперадмин>/<файлы суперадмина>

Локальная папка data/documents/ остаётся плоской — это кэш для конвейера docling,
структура ролей живёт в durable-хранилище (см. storage.doc_key).

Запуск на сервере:
    python seed_roles_demo.py            # завести роли и документы, показать отчёт
    python seed_roles_demo.py --report   # только отчёт, ничего не создавать
    python seed_roles_demo.py --purge    # убрать тестовые роли и их документы
"""

import sys
import secrets

import db
import users
import storage
import config
import indexing

# Логины тестовых учёток намеренно с префиксом test- : их легко найти и удалить.
DEMO_ADMINS = [
    {"username": "test-admin-log", "full_name": "Тестовый администратор — Логистика",
     "department": "Логистика", "position": "Начальник отдела логистики"},
    {"username": "test-admin-weld", "full_name": "Тестовый администратор — Сварка",
     "department": "Сварочный участок", "position": "Начальник участка"},
]
DEMO_EMPLOYEES = [
    {"username": "test-emp-driver", "full_name": "Тестовый сотрудник — Водитель",
     "department": "Логистика", "position": "Водитель автомобиля"},
    {"username": "test-emp-welder", "full_name": "Тестовый сотрудник — Сварщик",
     "department": "Сварочный участок", "position": "Электрогазосварщик"},
    {"username": "test-emp-newbie", "full_name": "Тестовый сотрудник — без отдела",
     "department": "", "position": "Стажёр"},
]
# Документы: по два на администратора. Короткие .md, чтобы не гонять docling.
DEMO_DOCS = {
    "test-admin-log": [
        ("test_log_marshruty.md",
         "# Регламент маршрутов\n\nВодитель получает путевой лист у диспетчера до выезда.\n"
         "Предрейсовый медосмотр обязателен. Отклонение от маршрута согласуется с логистом.\n"),
        ("test_log_pogruzka.md",
         "# Правила погрузки\n\nГруз крепится ремнями. Перегруз оси запрещён.\n"
         "Перед выездом водитель проверяет крепление и пломбы.\n"),
    ],
    "test-admin-weld": [
        ("test_weld_dopusk.md",
         "# Допуск к сварочным работам\n\nСварщик проходит аттестацию НАКС и инструктаж по ОТ.\n"
         "Наряд-допуск оформляется на каждый огневой участок.\n"),
        ("test_weld_siz.md",
         "# СИЗ сварщика\n\nКрага, щиток с автозатемнением, костюм с огнестойкой пропиткой.\n"
         "СИЗ выдаются под подпись в личной карточке.\n"),
    ],
}
OWNER_DOC = ("test_obshee_pvtr.md",
             "# Общий документ суперадмина\n\nПравила внутреннего трудового распорядка:\n"
             "начало рабочего дня 09:00, перерыв 13:00-14:00.\n")


def _get_or_create(spec: dict, role: str) -> tuple:
    """Возвращает (пользователь, выданный пароль или None, создан ли заново)."""
    existing = users.get_by_username(spec["username"])
    if existing:
        return existing, None, False
    password = secrets.token_urlsafe(9)
    user = users.create_user({**spec, "password": password}, role=users.ROLE_EMPLOYEE)
    if role != users.ROLE_EMPLOYEE:
        user = users.set_role(user["id"], role)
    return user, password, True


def seed() -> dict:
    owner = users.get_owner()
    if owner is None:
        raise SystemExit("Суперадмин (owner) не найден — запустите приложение хотя бы раз.")

    created = []
    admins = []
    for spec in DEMO_ADMINS:
        user, pwd, is_new = _get_or_create(spec, users.ROLE_ADMIN)
        admins.append(user)
        if is_new:
            created.append((user["username"], pwd, "администратор"))
    for spec in DEMO_EMPLOYEES:
        user, pwd, is_new = _get_or_create(spec, users.ROLE_EMPLOYEE)
        if is_new:
            created.append((user["username"], pwd, "сотрудник"))

    # Документы: каждый — от имени своего владельца, чтобы лечь в его папку.
    uploaded = []
    registry = {d.get("filename") for d in indexing.list_documents()}
    for admin in admins:
        for name, text in DEMO_DOCS.get(admin["username"], []):
            if name in registry:
                continue
            indexing.save_uploaded_file(name, text.encode("utf-8"), uploader=admin)
            uploaded.append((name, admin["username"]))
    if OWNER_DOC[0] not in registry:
        indexing.save_uploaded_file(OWNER_DOC[0], OWNER_DOC[1].encode("utf-8"), uploader=owner)
        uploaded.append((OWNER_DOC[0], owner.get("username")))

    return {"owner": owner, "admins": admins, "created": created, "uploaded": uploaded}


def purge() -> int:
    """Удаляет тестовые документы и тестовые учётки (только с префиксом test-)."""
    removed = 0
    for name in [n for pair in DEMO_DOCS.values() for n, _ in pair] + [OWNER_DOC[0]]:
        if indexing.delete_document(name):
            removed += 1
    for spec in DEMO_ADMINS + DEMO_EMPLOYEES:
        user = users.get_by_username(spec["username"])
        if user:
            users.delete_user(user["id"])
    return removed


def report():
    """Кто что видит + где физически лежат файлы."""
    owner = users.get_owner()
    all_users = users.list_users()
    docs = indexing.list_documents()

    print("=" * 78)
    print("РОЛИ И ВИДИМОСТЬ")
    print("=" * 78)
    actors = [owner] + [u for u in all_users if u.get("role") == users.ROLE_ADMIN]
    for actor in actors:
        if not actor:
            continue
        seen_docs = users.visible_docs(actor, docs)
        seen_people = users.visible_users(actor, all_users)
        title = "СУПЕРАДМИН" if users.is_owner(actor) else "администратор"
        print(f"\n[{title}] {actor.get('username')} · отдел: {actor.get('department') or '—'}")
        print(f"  документов видит: {len(seen_docs)} из {len(docs)}")
        for d in seen_docs:
            print(f"    · {d.get('filename')}  ->  {d.get('storage_path') or '(без владельца)'}")
        print(f"  людей видит: {len(seen_people)} из {len(all_users)}"
              f" ({', '.join(u.get('username') or u.get('full_name') or '?' for u in seen_people)})")

    employees = [u for u in all_users if u.get("role") == users.ROLE_EMPLOYEE]
    print(f"\n[сотрудники] {len(employees)} шт. — админских экранов не видят вовсе "
          f"(закрыты require_admin), им доступен только чат и своё расписание.")

    print("\n" + "=" * 78)
    print("ХРАНИЛИЩЕ ОРИГИНАЛОВ")
    print("=" * 78)
    if not config.S3_ENABLED:
        print("S3 выключен — оригиналы только локально в data/documents/ (плоский кэш).")
        print("Ключи, которые были бы созданы в S3, видны в колонке storage_path выше.")
        return
    listing = storage.list_objects(prefix=config.S3_PREFIX, delimiter="")
    print(f"{listing['endpoint']} · бакет {listing['bucket']} · префикс {config.S3_PREFIX!r}")
    tree: dict = {}
    for f in listing["files"]:
        rel = f["key"][len(config.S3_PREFIX):]
        parts = rel.split("/")
        folder = "/".join(parts[:-1]) or "(корень, файлы до разделения прав)"
        tree.setdefault(folder, []).append((parts[-1], f["size"]))
    for folder in sorted(tree):
        print(f"\n  {folder}/")
        for name, size in sorted(tree[folder]):
            print(f"      {name}  ({size} Б)")
    print(f"\n  всего объектов: {listing['count']}")


if __name__ == "__main__":
    db.init_schema()
    if "--purge" in sys.argv:
        print(f"Удалено тестовых документов: {purge()}; тестовые учётки удалены.")
        sys.exit(0)
    if "--report" not in sys.argv:
        result = seed()
        if result["created"]:
            print("Созданы тестовые учётные записи (пароли показываются ОДИН раз):")
            for username, password, role in result["created"]:
                print(f"  {role:<15} {username:<18} пароль: {password}")
        else:
            print("Тестовые учётные записи уже существуют — пароли не менялись.")
        print(f"Загружено тестовых документов: {len(result['uploaded'])}")
        print()
    report()
