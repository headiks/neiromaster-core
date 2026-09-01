"""
Оркестрация пайплайна: docling -> префильтр -> проход 1 (карточка) -> проход 2 (разметка
секций) -> мелкий чанкинг -> эмбеддинги -> запись в PG и Qdrant. Плюс асинхронная очередь
разметки с retry/backoff и возобновлением с последней размеченной секции.

Публичный API: ingest, reindex, relabel_candidates, retrieve, enqueue, worker управление.
"""

import time
import queue
import hashlib
import threading
from pathlib import Path

from config import get_embedding
from . import core, llm, store, professions, qdrant_sink
from .qdrant_sink import EMBED_VERSION


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


# ---------- Разбор docling: секции по заголовкам + страницы для колонтитулов ----------
def _parse(filepath: Path):
    """Возвращает (title, toc, head_text, sections, repeated, docling_version).
    Переиспуем конвертер/чанкер из indexing (тот же docling, с кэшом разбора)."""
    import indexing
    import docling
    doc = indexing.convert_document(filepath)

    # Секции = чанки HybridChunker (уже сгруппированы по заголовкам), при необходимости дробим.
    sections = []
    for ch in indexing.chunker.chunk(doc):
        heading_path = [h for h in (getattr(ch.meta, "headings", None) or []) if h]
        page = indexing.extract_page_no(ch)
        for piece in core.split_section_text(ch.text, max_tokens=1200):
            sections.append({"heading_path": heading_path, "text": piece,
                             "page_from": page, "page_to": page})

    # Страницы (для детекции колонтитулов) — из сырых текстовых элементов документа.
    pages = {}
    for t in getattr(doc, "texts", None) or []:
        try:
            pg = t.prov[0].page_no
        except Exception:
            pg = None
        line = (getattr(t, "text", "") or "").strip()
        if line:
            pages.setdefault(pg, []).append(line)
    repeated = core.repeated_lines([pages[k] for k in sorted(pages, key=lambda x: (x is None, x))])

    title = filepath.name
    md = doc.export_to_markdown()
    toc = "\n".join(l for l in md.splitlines() if l.startswith("#"))[:2000]
    head_text = md[:12000]
    docling_version = getattr(docling, "__version__", "docling")
    return title, toc, head_text, sections, repeated, docling_version


def _label_section(sec: dict, repeated: set, card: dict, structure: dict, positions: list) -> dict:
    """Метка одной секции: префильтр -> (при прохождении) LLM -> нормализация + матч профессий."""
    text = sec.get("text") or ""
    ok, reason = core.prefilter(text)
    if ok and text.strip() in repeated:
        ok, reason = False, "running_header"
    if not ok:
        return {"is_meaningful": False, "reject_reason": reason, "substages": [], "stages": [],
                "professions": [], "is_general": False, "prof_conf": None, "why": None}

    raw = llm.section_labels(text, sec.get("heading_path") or [], card, structure, positions)
    norm = core.coerce_section_labels(raw, structure)
    matched, prof_conf = professions.match_to_staffing(norm["professions"], positions)
    norm["professions"] = matched
    norm["is_general"] = norm["is_general"] and not matched
    norm["prof_conf"] = prof_conf
    norm["reject_reason"] = None
    return norm


def ingest(filepath, filename: str = None, plan_version: str = "current",
           job_id: str = None, force: bool = False) -> dict:
    """Полный приём одного документа. Идемпотентно по content_hash (force — переразбор).
    job_id — если передан, прогресс пишется в задачу и разметка возобновляется с last_seq."""
    filepath = Path(filepath)
    filename = filename or filepath.name
    data = filepath.read_bytes()
    content_hash = _hash_bytes(data)

    existing = store.find_by_hash(content_hash)
    if existing and not force:
        return {"doc_id": existing["id"], "status": "unchanged", "content_hash": content_hash}

    structure = store.get_plan_structure(plan_version)
    positions = professions.staffing_positions()

    title, toc, head_text, sections, repeated, docling_version = _parse(filepath)
    card = llm.doc_card(title, toc, head_text)
    doc_id, _ = store.upsert_document(filename, content_hash, card, docling_version, llm.MODEL)
    section_ids = store.replace_sections(doc_id, sections)

    if job_id is None:
        job_id = store.create_job(doc_id, filename, total=len(sections))
    store.update_job(job_id, status="running", total=len(sections))
    resume_from = (store.get_job(job_id) or {}).get("last_seq", -1)

    for seq, (sec, sid) in enumerate(zip(sections, section_ids)):
        if seq <= resume_from:
            continue                                   # уже размечено — возобновление
        label = _label_section(sec, repeated, card, structure, positions)
        store.upsert_section_label(sid, label, source="llm", plan_version=plan_version,
                                   model=llm.MODEL, prompt_version=llm.PROMPT_VERSION)
        store.replace_chunks(sid, core.to_chunks(sec["text"]), EMBED_VERSION)
        store.update_job(job_id, done=seq + 1, last_seq=seq)

    qdrant_sink.sink_document(doc_id)                  # запись производной копии в Qdrant
    store.update_job(job_id, status="done")
    return {"doc_id": doc_id, "status": "indexed", "sections": len(sections), "content_hash": content_hash}


def reindex() -> dict:
    """Пересборка Qdrant из PG без обращений к LLM."""
    return qdrant_sink.reindex()


def relabel_candidates(substage_id: str, plan_version: str = "current", top_k: int = 3000) -> dict:
    """При добавлении подэтапа: отбираем топ-K секций по косинусу к его описанию и
    переразмечаем только их (правки человека не трогаем)."""
    structure = store.get_plan_structure(plan_version)
    positions = professions.staffing_positions()
    # описание подэтапа как запрос
    query = substage_id
    for st in structure.get("stages") or []:
        for sub in st.get("substages") or []:
            if sub.get("id") == substage_id:
                query = f"{st.get('title')} {sub.get('title')} {sub.get('description') or sub.get('brief') or ''}"
    vec = get_embedding(query)
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    hits = qdrant_sink.client.query_points(
        qdrant_sink.COLLECTION, query=vec, limit=top_k, with_payload=True,
        query_filter=Filter(must=[FieldCondition(key="level", match=MatchValue(value="section"))]),
    ).points
    section_ids = {(h.payload or {}).get("section_id") for h in hits if (h.payload or {}).get("section_id")}

    touched = 0
    for sid in section_ids:
        if store.get_label_source(sid) == "human":
            continue                                   # правки человека не трогаем
        sec = _section_row(sid)
        if not sec:
            continue
        label = _label_section(sec, set(), _doc_card_for(sec["doc_id"]), structure, positions)
        store.upsert_section_label(sid, label, source="llm", plan_version=plan_version,
                                   model=llm.MODEL, prompt_version=llm.PROMPT_VERSION)
        touched += 1
    qdrant_sink.reindex()
    return {"relabeled": touched, "candidates": len(section_ids)}


def _section_row(section_id: str):
    import db
    r = db.query("SELECT id, doc_id, heading_path, text, page_from, page_to FROM sections WHERE id = %s",
                 (section_id,), fetch="one")
    return dict(r) if r else None


def _doc_card_for(doc_id: str) -> dict:
    import db
    r = db.query("SELECT doc_card FROM documents WHERE id = %s", (doc_id,), fetch="one")
    return (r or {}).get("doc_card") or {}


def retrieve(substage_id: str, position: str = "", plan_version: str = "current", limit: int = 8) -> list:
    return qdrant_sink.retrieve(substage_id, position, plan_version, limit)


# ---------- Асинхронная очередь разметки (retry + backoff + возобновление) ----------
_queue: "queue.Queue" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
MAX_ATTEMPTS = 3


def _worker():
    while True:
        task = _queue.get()
        filepath, filename, job_id = task["filepath"], task["filename"], task["job_id"]
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            try:
                ingest(filepath, filename=filename, job_id=job_id)
                break
            except Exception as e:
                attempt += 1
                store.update_job(job_id, status="error", attempts=attempt, error=str(e))
                if attempt >= MAX_ATTEMPTS:
                    break
                time.sleep(2 ** attempt)               # backoff; возобновление с last_seq в ingest
        _queue.task_done()


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, name="docpipe-worker", daemon=True).start()
            _worker_started = True


def enqueue(filepath, filename: str = None) -> str:
    """Ставит документ в очередь разметки. Возвращает job_id; прогресс — store.get_job(job_id)."""
    _ensure_worker()
    filepath = Path(filepath)
    data = filepath.read_bytes()
    existing = store.find_by_hash(_hash_bytes(data))
    doc_id = existing["id"] if existing else None
    job_id = store.create_job(doc_id, filename or filepath.name, total=0)
    _queue.put({"filepath": str(filepath), "filename": filename or filepath.name, "job_id": job_id})
    return job_id
