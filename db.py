"""
Хранилище на PostgreSQL — единая база структурированных данных приложения
(аккаунты; далее сюда же переносятся вопросы, реестр документов, планы).

Почему PostgreSQL:
  - рассчитан на большое число пользователей и одновременных подключений
    (несколько uvicorn-воркеров / серверов приложения работают с одной БД);
  - ACID-транзакции, честные типы (BOOLEAN, TIMESTAMP), внешние ключи, индексы;
  - пул соединений (psycopg_pool) держит подключения открытыми — не платим за
    установку соединения на каждый запрос.

Подключение — через DSN из окружения. По умолчанию — локальный Postgres:
    postgresql://neiromaster:neiromaster@localhost:5432/neiromaster
Переопределяется переменной NEIROMASTER_DB_DSN (или DATABASE_URL).

Схема идемпотентна (CREATE TABLE IF NOT EXISTS) — init_schema() безопасно звать при
каждом старте.
"""

import os
import re
import threading
import contextlib

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

DSN = (
    os.environ.get("NEIROMASTER_DB_DSN")
    or os.environ.get("DATABASE_URL")
    or "postgresql://neiromaster:neiromaster@localhost:5432/neiromaster"
)

# Верхняя граница пула. Для многих пользователей упирается не в число людей, а в
# число одновременных запросов; при нескольких воркерах учитывайте суммарный лимит
# max_connections у Postgres. Настраивается переменной NEIROMASTER_DB_POOL.
POOL_MAX = int(os.environ.get("NEIROMASTER_DB_POOL", "10"))

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

# --- Мультитенантность «схема на кабинет» ---
# Текущая схема хранится в thread-local, а search_path выставляется на КАЖДОМ
# соединении при получении из пула (пул переиспользует коннекты — без установки
# на каждый чекаут схема «протекла» бы между запросами). Без контекста use_schema
# всё работает в public, как раньше (обратная совместимость).
# ponytail: SET search_path на каждый вызов query/execute; если станет узким местом —
# перейти на pool reset-hook или отдельный пул на схему.
_local = threading.local()
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")   # валидное и безопасное имя схемы


@contextlib.contextmanager
def use_schema(name: str):
    """В пределах блока все query/execute/init_schema идут в указанную схему."""
    if not _SCHEMA_RE.match(name):
        raise ValueError(f"Недопустимое имя схемы: {name!r}")
    prev = getattr(_local, "schema", None)
    _local.schema = name
    try:
        yield
    finally:
        _local.schema = prev


def _apply_schema(conn):
    """Выставить search_path соединения под текущую схему (или public по умолчанию)."""
    schema = getattr(_local, "schema", None)
    if schema:
        conn.execute(f'SET search_path TO "{schema}", public')
    else:
        conn.execute("SET search_path TO public")

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id                       TEXT PRIMARY KEY,
        username                 TEXT UNIQUE,
        full_name                TEXT    NOT NULL DEFAULT '',
        role                     TEXT    NOT NULL DEFAULT 'employee',
        active                   BOOLEAN NOT NULL DEFAULT TRUE,
        salt                     TEXT,
        hash                     TEXT,
        must_change_credentials  BOOLEAN NOT NULL DEFAULT FALSE,
        created_at               TEXT,
        updated_at               TEXT,
        password_changed_at      TEXT,
        position                 TEXT DEFAULT '',
        department               TEXT DEFAULT '',
        contact                  TEXT DEFAULT '',
        mentor                   TEXT DEFAULT '',
        manager                  TEXT DEFAULT '',
        plan_id                  TEXT,
        start_date               TEXT,
        status                   TEXT DEFAULT 'planned',
        notes                    TEXT DEFAULT '',
        created_by               TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS idx_users_role     ON users(role)",
    # Сессии входа. В БД, а не в памяти процесса: переживают перезапуск приложения
    # (пользователей не разлогинивает) и общие для всех uvicorn-воркеров.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token       TEXT PRIMARY KEY,
        user_id     TEXT             NOT NULL,
        created_at  DOUBLE PRECISION NOT NULL,
        seen_at     DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    # Смысловые папки — логические категории базы знаний, которыми управляет
    # ТОЛЬКО человек (ИИ классифицирует документы внутрь, но не создаёт папки).
    # Папка не хранит файлов: criteria — набор смысловых критериев отнесения,
    # stage_ids — связанные этапы обучения. Оба поля JSONB-массивы строк.
    """
    CREATE TABLE IF NOT EXISTS folders (
        id          TEXT PRIMARY KEY,
        slug        TEXT UNIQUE NOT NULL,
        name        TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        criteria    JSONB   NOT NULL DEFAULT '[]',
        stage_ids   JSONB   NOT NULL DEFAULT '[]',
        enabled     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at  TEXT,
        updated_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_folders_enabled ON folders(enabled)",
    # Этапы обучения (блоки) — «структура обучения» из ТЗ. Задаёт контекст текущего
    # этапа пользователя и цель для связей папок (folders.stage_ids). Подэтапы вынесены
    # в отдельную таблицу substages (связь по stage_id), а не JSONB внутри строки.
    """
    CREATE TABLE IF NOT EXISTS stages (
        id          TEXT PRIMARY KEY,
        title       TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        position    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT,
        updated_at  TEXT
    )
    """,
    # Подэтапы этапа. Отдельная таблица (а не JSONB в stages): на подэтап по id
    # ссылаются documents.substages и планировщик, поэтому нужен стабильный PK и FK.
    # ON DELETE CASCADE — удаление этапа уносит его подэтапы (как было при JSONB).
    """
    CREATE TABLE IF NOT EXISTS substages (
        id          TEXT PRIMARY KEY,
        stage_id    TEXT    NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
        title       TEXT    NOT NULL,
        position    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT,
        updated_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_substages_stage ON substages(stage_id, position)",
)


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    DSN, min_size=1, max_size=POOL_MAX,
                    kwargs={"row_factory": dict_row}, open=True,
                )
    return _pool


def configure(dsn: str):
    """Переключить БД (используется в тестах). Закрывает прежний пул."""
    global _pool, DSN
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
        DSN = dsn


def init_schema():
    """Создаёт таблицы и индексы, если их ещё нет (в текущей схеме, см. use_schema)."""
    with _get_pool().connection() as conn:
        _apply_schema(conn)
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
    _migrate_substages_from_jsonb()


def _migrate_substages_from_jsonb():
    """Однократный перенос подэтапов из старой колонки stages.substages (JSONB) в
    таблицу substages. Идемпотентно: как только колонка удалена — ничего не делает.
    Ограничен ТЕКУЩЕЙ схемой (current_schema) — важно для мультитенантности."""
    with _get_pool().connection() as conn:
        _apply_schema(conn)
        has_col = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'stages' AND column_name = 'substages'"
        ).fetchone()
        if not has_col:
            return
        for r in conn.execute("SELECT id, substages FROM stages").fetchall():
            for pos, s in enumerate(r["substages"] or []):
                if not isinstance(s, dict) or not s.get("id"):
                    continue
                conn.execute(
                    "INSERT INTO substages (id, stage_id, title, position, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, now()::text, now()::text) ON CONFLICT (id) DO NOTHING",
                    (s["id"], r["id"], s.get("title", ""), pos),
                )
        conn.execute("ALTER TABLE stages DROP COLUMN substages")


def query(sql: str, params: tuple = (), fetch: str = "all"):
    """SELECT-запрос. fetch: 'all' -> список строк, 'one' -> одна строка/None."""
    with _get_pool().connection() as conn:
        _apply_schema(conn)
        cur = conn.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return None


def execute(sql: str, params: tuple = ()):
    """INSERT/UPDATE/DELETE. Коммит — при выходе из контекста соединения."""
    with _get_pool().connection() as conn:
        _apply_schema(conn)
        conn.execute(sql, params)
