"""
provisioning.py — создание изолированного кабинета компании.

Модель мультитенантности: «схема на кабинет» (schema-per-tenant). Каждая компания
получает свою PostgreSQL-схему со своим полным набором таблиц — данные клиентов
физически изолированы в одной базе. Подходит для десятков-сотен кабинетов.

Kafka здесь не нужна: создание кабинета — редкая синхронная операция с немедленным
результатом («схема создалась / нет»), а не поток событий. Всё делается одним
рукописным скриптом поверх уже существующих init_schema (db, docpipe, documents).

    python provisioning.py <slug> [--company "ООО Ромашка"]   # создать кабинет
    python provisioning.py --list                              # список кабинетов

Что делает provision_cabinet():
    CREATE SCHEMA "cab_<slug>"
      -> все CREATE TABLE в этой схеме (db + docpipe + document_meta)
      -> посев стартовой структуры (этапы + папки) из knowledge_seed.json
      -> регистрация кабинета в реестре public.cabinets
"""
import re
import sys
import time
import json

import db
import stages
import folders
import documents
from config import BASE_DIR

SCHEMA_PREFIX = "cab_"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,40}$")


def schema_for(slug: str) -> str:
    """slug кабинета -> имя схемы. Валидирует slug (латиница/цифры/подчёркивание)."""
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError("slug кабинета: латиница/цифры/подчёркивание, начинается с буквы или цифры")
    return f"{SCHEMA_PREFIX}{slug}"


def ensure_registry():
    """Реестр кабинетов в public — общий для всех тенантов (какие кабинеты есть)."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS public.cabinets ("
        "  slug        TEXT PRIMARY KEY,"
        "  schema_name TEXT UNIQUE NOT NULL,"
        "  company     TEXT NOT NULL DEFAULT '',"
        "  created_at  TEXT NOT NULL)"
    )


def cabinet_exists(schema: str) -> bool:
    r = db.query(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
        (schema,), fetch="one",
    )
    return r is not None


def list_cabinets() -> list:
    ensure_registry()
    return db.query("SELECT slug, schema_name, company, created_at FROM public.cabinets ORDER BY created_at")


def provision_cabinet(slug: str, company: str = "", seed_path=None) -> str:
    """Создать кабинет: схема + все таблицы + посев + запись в реестр. Возвращает имя схемы."""
    schema = schema_for(slug)
    ensure_registry()
    if cabinet_exists(schema):
        raise ValueError(f"Кабинет уже существует: схема {schema!r}")

    # 1) сама схема
    with db._get_pool().connection() as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    # 2) все таблицы + посев — внутри схемы кабинета (search_path)
    with db.use_schema(schema):
        db.init_schema()                      # users, sessions, folders, stages, substages
        try:
            import docpipe
            docpipe.init_schema()             # конвейер разметки v2
        except Exception as e:
            print(f"[warn] docpipe schema в {schema}: {e}")
        documents.init()                      # document_meta (реестр v1)

        seed_path = seed_path or (BASE_DIR / "data" / "knowledge_seed.json")
        if seed_path.exists():
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
            s = stages.seed_if_empty(seed.get("stages", []))
            f = folders.seed_if_empty(seed.get("folders", []))
            print(f"[seed] {schema}: этапов {s}, папок {f}")
        else:
            print(f"[warn] нет файла сида {seed_path} — кабинет создан без стартовой структуры")

    # 3) регистрация в общем реестре (public)
    db.execute(
        "INSERT INTO public.cabinets (slug, schema_name, company, created_at) VALUES (%s, %s, %s, %s)",
        (slug.strip().lower(), schema, company or "", time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    return schema


def _main(argv):
    if "--list" in argv:
        rows = list_cabinets()
        if not rows:
            print("Кабинетов пока нет.")
        for r in rows:
            print(f"  {r['slug']:<20} {r['schema_name']:<24} {r['company']}  ({r['created_at']})")
        return
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return
    slug = args[0]
    company = ""
    if "--company" in argv:
        i = argv.index("--company")
        company = argv[i + 1] if i + 1 < len(argv) else ""
    schema = provision_cabinet(slug, company=company)
    print(f"Кабинет создан: схема {schema}")


def _selfcheck():
    """Смоук-тест валидации имени схемы без БД."""
    assert schema_for("acme") == "cab_acme"
    assert schema_for(" Acme ") == "cab_acme"
    for bad in ("", "1acme-x", "acme;drop", "переезд"):
        try:
            schema_for(bad)
            raise AssertionError(f"должно было упасть: {bad!r}")
        except ValueError:
            pass
    print("provisioning: schema_for — OK")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _main(sys.argv[1:])
    else:
        _selfcheck()
