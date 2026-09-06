"""
Тесты системы ролей: кто что видит и где физически лежат документы.

Проверяем без БД и сети (db заглушён): правила видимости — чистые функции в
users.py, раскладка хранилища — storage.doc_key.

    суперадмин (owner) — все документы всех администраторов, все люди;
    администратор      — только свои загрузки и люди своего отдела;
    сотрудник          — админских ручек не видит вовсе (require_admin).

Запуск:  python test_roles.py   ИЛИ   pytest test_roles.py
"""

import sys
import types

# ---- заглушки тяжёлых зависимостей ДО импорта users/storage ----
for _n in ("psycopg", "psycopg.rows", "psycopg_pool", "requests"):
    sys.modules.setdefault(_n, types.ModuleType(_n))
sys.modules["psycopg.rows"].dict_row = object
sys.modules["psycopg_pool"].ConnectionPool = object
_db = types.ModuleType("db")
_db.execute = lambda *a, **k: None
_db.query = lambda *a, **k: None
sys.modules["db"] = _db

import users
import storage
import config

OWNER = {"id": "own", "username": "super", "role": "owner", "department": ""}
ADMIN_LOG = {"id": "a1", "username": "adm.log", "role": "admin", "department": "Логистика"}
ADMIN_SVAR = {"id": "a2", "username": "adm.svar", "role": "admin", "department": "Сварка"}
EMP_LOG = {"id": "e1", "username": "driver", "role": "employee", "department": "логистика"}
EMP_SVAR = {"id": "e2", "username": "welder", "role": "employee", "department": "Сварка"}
EMP_NONE = {"id": "e3", "username": "nobody", "role": "employee", "department": ""}
ALL_USERS = [OWNER, ADMIN_LOG, ADMIN_SVAR, EMP_LOG, EMP_SVAR, EMP_NONE]

DOCS = [
    {"filename": "ot_log.pdf", "uploaded_by": "a1"},
    {"filename": "marshruty.docx", "uploaded_by": "a1"},
    {"filename": "svarka.pdf", "uploaded_by": "a2"},
    {"filename": "obshee.md", "uploaded_by": "own"},
    {"filename": "legacy.pdf"},                       # залит до разделения прав
]


# ---------- Видимость документов ----------
def test_owner_sees_every_document():
    assert len(users.visible_docs(OWNER, DOCS)) == len(DOCS)


def test_admin_sees_only_own_uploads():
    assert [d["filename"] for d in users.visible_docs(ADMIN_LOG, DOCS)] == ["ot_log.pdf", "marshruty.docx"]
    assert [d["filename"] for d in users.visible_docs(ADMIN_SVAR, DOCS)] == ["svarka.pdf"]


def test_admin_does_not_see_ownerless_docs():
    # «ничьи» документы видит только суперадмин — иначе старая база утекла бы всем админам
    assert not users.can_see_doc(ADMIN_LOG, {"filename": "legacy.pdf"})
    assert users.can_see_doc(OWNER, {"filename": "legacy.pdf"})


# ---------- Видимость людей ----------
def test_owner_sees_all_people():
    assert len(users.visible_users(OWNER, ALL_USERS)) == len(ALL_USERS)


def test_admin_sees_only_his_department():
    seen = {u["id"] for u in users.visible_users(ADMIN_LOG, ALL_USERS)}
    assert seen == {"a1", "e1"}                     # свой отдел (регистр не важен) + сам
    assert "e2" not in seen and "e3" not in seen and "own" not in seen


def test_admin_manages_only_his_department_employees():
    assert users.can_manage(ADMIN_LOG, EMP_LOG)
    assert not users.can_manage(ADMIN_LOG, EMP_SVAR)     # чужой отдел
    assert not users.can_manage(ADMIN_LOG, EMP_NONE)     # отдел не проставлен
    assert not users.can_manage(ADMIN_LOG, ADMIN_SVAR)   # другой администратор
    assert not users.can_manage(ADMIN_LOG, OWNER)
    assert users.can_manage(OWNER, ADMIN_LOG) and users.can_manage(OWNER, EMP_SVAR)


def test_employee_manages_nobody():
    assert not users.can_manage(EMP_LOG, EMP_LOG)
    assert not users.is_admin(EMP_LOG)


# ---------- Раскладка хранилища ----------
def test_storage_tree_is_owner_then_admin_then_file():
    config.S3_PREFIX = "documents/"
    top = users.dir_slug(OWNER)
    assert storage.doc_key("ot.pdf", top, users.dir_slug(ADMIN_LOG)) == "documents/super/adm.log/ot.pdf"
    assert storage.doc_key("s.pdf", top, users.dir_slug(ADMIN_SVAR)) == "documents/super/adm.svar/s.pdf"
    # документы самого суперадмина лежат в его же папке внутри его каталога
    assert storage.doc_key("o.md", top, top) == "documents/super/super/o.md"


def test_storage_key_takes_basename_only():
    """Путь в имени файла не должен выводить ключ из папки администратора."""
    config.S3_PREFIX = "documents/"
    key = storage.doc_key("../../etc/passwd", "super", "adm.log")
    assert key == "documents/super/adm.log/passwd"


def test_storage_key_falls_back_to_flat_for_legacy():
    config.S3_PREFIX = "documents/"
    assert storage.doc_key("old.pdf") == "documents/old.pdf"


def test_dir_slug_is_safe_path_segment():
    allowed = users.USERNAME_ALLOWED
    assert set(users.dir_slug(ADMIN_LOG)) <= allowed
    assert users.dir_slug({"id": "1234567890ab"}) == "user-12345678"
    assert "/" not in users.dir_slug({"id": "x/y"})


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK ", name)
    print("test_roles: все проверки пройдены")
