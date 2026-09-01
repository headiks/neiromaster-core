"""
CRUD слоя PostgreSQL (источник правды). Здесь же — идемпотентность по content_hash и
защита правок человека (source=human не затирается при перепрогоне).
"""

import json
import uuid

from psycopg.types.json import Json

import db


def _id() -> str:
    return uuid.uuid4().hex


# ---------- Версии плана ----------
def save_plan_version(structure: dict, version_id: str = "current") -> str:
    db.execute(
        "INSERT INTO plan_versions (id, structure) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET structure = EXCLUDED.structure",
        (version_id, Json(structure or {})),
    )
    return version_id


def get_plan_structure(version_id: str = "current") -> dict:
    r = db.query("SELECT structure FROM plan_versions WHERE id = %s", (version_id,), fetch="one")
    return (r or {}).get("structure") or {}


# ---------- Документы (идемпотентность по content_hash) ----------
def find_by_hash(content_hash: str):
    return db.query("SELECT id FROM documents WHERE content_hash = %s", (content_hash,), fetch="one")


def upsert_document(filename: str, content_hash: str, card: dict,
                    docling_version: str, card_model: str) -> tuple:
    """Возвращает (doc_id, changed). Неизменившийся файл (тот же hash) не пересоздаётся —
    changed=False, разбор/разметку можно пропустить."""
    existing = find_by_hash(content_hash)
    if existing:
        return existing["id"], False
    doc_id = _id()
    db.execute(
        "INSERT INTO documents (id, filename, content_hash, doc_card, docling_version, card_model) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (doc_id, filename, content_hash, Json(card or {}), docling_version, card_model),
    )
    return doc_id, True


def delete_document(doc_id: str):
    db.execute("DELETE FROM documents WHERE id = %s", (doc_id,))   # каскадом снесёт секции/метки/чанки


# ---------- Секции ----------
def replace_sections(doc_id: str, sections: list) -> list:
    """sections — [{heading_path, text, page_from, page_to}]. Возвращает список id (по порядку)."""
    db.execute("DELETE FROM sections WHERE doc_id = %s", (doc_id,))
    ids = []
    for seq, s in enumerate(sections):
        sid = _id()
        db.execute(
            "INSERT INTO sections (id, doc_id, seq, heading_path, text, page_from, page_to) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (sid, doc_id, seq, list(s.get("heading_path") or []), s.get("text") or "",
             s.get("page_from"), s.get("page_to")),
        )
        ids.append(sid)
    return ids


def list_sections(doc_id: str) -> list:
    return db.query("SELECT * FROM sections WHERE doc_id = %s ORDER BY seq", (doc_id,))


# ---------- Метки секций (правки человека не затираем) ----------
def get_label_source(section_id: str):
    r = db.query("SELECT source FROM section_labels WHERE section_id = %s", (section_id,), fetch="one")
    return (r or {}).get("source")


def upsert_section_label(section_id: str, label: dict, source: str,
                         plan_version: str, model: str, prompt_version: str) -> bool:
    """Пишет метку секции. Если текущая метка source=human — НЕ трогаем (возвращаем False)."""
    if get_label_source(section_id) == "human":
        return False
    db.execute(
        """INSERT INTO section_labels
             (section_id, is_meaningful, reject_reason, substages, stages, professions,
              is_general, prof_conf, why, source, plan_version, model, prompt_version, labeled_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (section_id) DO UPDATE SET
             is_meaningful=EXCLUDED.is_meaningful, reject_reason=EXCLUDED.reject_reason,
             substages=EXCLUDED.substages, stages=EXCLUDED.stages, professions=EXCLUDED.professions,
             is_general=EXCLUDED.is_general, prof_conf=EXCLUDED.prof_conf, why=EXCLUDED.why,
             source=EXCLUDED.source, plan_version=EXCLUDED.plan_version, model=EXCLUDED.model,
             prompt_version=EXCLUDED.prompt_version, labeled_at=now()""",
        (section_id, bool(label.get("is_meaningful", True)), label.get("reject_reason"),
         Json(label.get("substages") or []), list(label.get("stages") or []),
         list(label.get("professions") or []), bool(label.get("is_general", False)),
         label.get("prof_conf"), label.get("why"), source, plan_version, model, prompt_version),
    )
    return True


def get_section_label(section_id: str):
    return db.query("SELECT * FROM section_labels WHERE section_id = %s", (section_id,), fetch="one")


# ---------- Чанки ----------
def replace_chunks(section_id: str, chunks: list, embedding_version: str) -> list:
    db.execute("DELETE FROM chunks WHERE section_id = %s", (section_id,))
    ids = []
    for seq, text in enumerate(chunks):
        cid = _id()
        db.execute(
            "INSERT INTO chunks (id, section_id, seq, text, embedding_version) VALUES (%s,%s,%s,%s,%s)",
            (cid, section_id, seq, text, embedding_version),
        )
        ids.append(cid)
    return ids


# ---------- Выборка для пересборки Qdrant ----------
def iter_labeled_sections():
    """Секции с метками + документ — для reindex. Метки чанков наследуются от секции."""
    return db.query(
        "SELECT s.id AS section_id, s.doc_id, s.heading_path, s.page_from, s.text, "
        "       l.is_meaningful, l.substages, l.stages, l.professions, l.is_general, l.plan_version "
        "FROM sections s JOIN section_labels l ON l.section_id = s.id"
    )


def list_chunks(section_id: str) -> list:
    return db.query("SELECT * FROM chunks WHERE section_id = %s ORDER BY seq", (section_id,))


# ---------- Задачи разметки (очередь, возобновление) ----------
def create_job(doc_id: str, filename: str, total: int) -> str:
    job_id = _id()
    db.execute("INSERT INTO label_jobs (id, doc_id, filename, status, total) VALUES (%s,%s,%s,'queued',%s)",
               (job_id, doc_id, filename, total))
    return job_id


def update_job(job_id: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    db.execute(f"UPDATE label_jobs SET {cols}, updated_at = now() WHERE id = %s",
               tuple(fields.values()) + (job_id,))


def get_job(job_id: str):
    return db.query("SELECT * FROM label_jobs WHERE id = %s", (job_id,), fetch="one")


def pending_jobs() -> list:
    return db.query("SELECT * FROM label_jobs WHERE status IN ('queued','running','error') ORDER BY updated_at")
