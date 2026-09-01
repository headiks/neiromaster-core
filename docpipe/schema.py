"""
Схема PostgreSQL для пайплайна разметки (источник правды). Qdrant — производная копия,
собирается из этих таблиц (см. qdrant_sink.reindex). Правило: ни одного поля в Qdrant,
которого нет здесь.

Идемпотентно (CREATE TABLE IF NOT EXISTS) — init_schema() безопасно звать при старте.
"""

import db

SCHEMA_STATEMENTS = (
    # Версии плана адаптации: структура этапов/подэтапов, относительно которой размечаем.
    """
    CREATE TABLE IF NOT EXISTS plan_versions (
        id         TEXT PRIMARY KEY,
        structure  JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Карточка документа (проход 1) + идемпотентность по content_hash.
    """
    CREATE TABLE IF NOT EXISTS documents (
        id              TEXT PRIMARY KEY,
        filename        TEXT NOT NULL,
        content_hash    TEXT NOT NULL,
        doc_card        JSONB NOT NULL DEFAULT '{}',
        docling_version TEXT,
        card_model      TEXT,
        parsed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)",
    # Секции (проход 2): текст по заголовкам уровня 2-3.
    """
    CREATE TABLE IF NOT EXISTS sections (
        id           TEXT PRIMARY KEY,
        doc_id       TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        seq          INTEGER NOT NULL DEFAULT 0,
        heading_path TEXT[] NOT NULL DEFAULT '{}',
        text         TEXT NOT NULL DEFAULT '',
        page_from    INTEGER,
        page_to      INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sections_doc ON sections(doc_id)",
    # Метки секции. source (llm|human|inherited) — правки человека не затираются.
    """
    CREATE TABLE IF NOT EXISTS section_labels (
        section_id    TEXT PRIMARY KEY REFERENCES sections(id) ON DELETE CASCADE,
        is_meaningful BOOLEAN NOT NULL DEFAULT TRUE,
        reject_reason TEXT,
        substages     JSONB NOT NULL DEFAULT '[]',
        stages        TEXT[] NOT NULL DEFAULT '{}',
        professions   TEXT[] NOT NULL DEFAULT '{}',
        is_general    BOOLEAN NOT NULL DEFAULT FALSE,
        prof_conf     REAL,
        why           TEXT,
        source        TEXT NOT NULL DEFAULT 'llm',
        plan_version  TEXT,
        model         TEXT,
        prompt_version TEXT,
        labeled_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Мелкие чанки: метки НЕ пересчитываются, наследуются от секции при сборке Qdrant.
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id                TEXT PRIMARY KEY,
        section_id        TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
        seq               INTEGER NOT NULL DEFAULT 0,
        text              TEXT NOT NULL DEFAULT '',
        embedding_version TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section_id)",
    # Очередь разметки: документ = задача, возобновляется с последней размеченной секции.
    """
    CREATE TABLE IF NOT EXISTS label_jobs (
        id           TEXT PRIMARY KEY,
        doc_id       TEXT,
        filename     TEXT,
        status       TEXT NOT NULL DEFAULT 'queued',
        total        INTEGER NOT NULL DEFAULT 0,
        done         INTEGER NOT NULL DEFAULT 0,
        attempts     INTEGER NOT NULL DEFAULT 0,
        last_seq     INTEGER NOT NULL DEFAULT -1,
        error        TEXT,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


def init_schema():
    for stmt in SCHEMA_STATEMENTS:
        db.execute(stmt)
