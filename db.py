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
import threading

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
    # Этапы обучения (блоки) и их подэтапы — «структура обучения» из ТЗ. Задаёт
    # контекст текущего этапа пользователя и цель для связей папок (folders.stage_ids).
    # substages — JSONB-массив объектов {id, title}.
    """
    CREATE TABLE IF NOT EXISTS stages (
        id          TEXT PRIMARY KEY,
        title       TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        substages   JSONB   NOT NULL DEFAULT '[]',
        position    INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT,
        updated_at  TEXT
    )
    """,
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
    """Создаёт таблицы и индексы, если их ещё нет."""
    with _get_pool().connection() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)


def query(sql: str, params: tuple = (), fetch: str = "all"):
    """SELECT-запрос. fetch: 'all' -> список строк, 'one' -> одна строка/None."""
    with _get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return None


def execute(sql: str, params: tuple = ()):
    """INSERT/UPDATE/DELETE. Коммит — при выходе из контекста соединения."""
    with _get_pool().connection() as conn:
        conn.execute(sql, params)
