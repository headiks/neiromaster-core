"""
Qdrant — ПРОИЗВОДНАЯ копия PG: пересобирается из БД без обращений к LLM. Одна коллекция,
level = section | chunk. В payload только фильтруемое (правило: ни одного поля, которого нет
в PG). reindex() строит её заново; retrieve() выбирает секции под подэтап и должность.
"""

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue, MatchAny,
    PayloadSchemaType,
)

from config import QDRANT_HOST, QDRANT_PORT, EMBED_DIM, get_embedding
from . import store

COLLECTION = "docpipe"
EMBED_VERSION = "bge-m3"
PROF_BOOST = 1.2       # мягкий приоритет своей профессии
RETRIEVE_MIN = 3       # меньше — добираем общими

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

# keyword — на массивы/строки, bool — на флаги, keyword на plan_version (id текстовый, см. PG).
_INDEXES = {
    "doc_id": PayloadSchemaType.KEYWORD,
    "level": PayloadSchemaType.KEYWORD,
    "substages": PayloadSchemaType.KEYWORD,
    "stages": PayloadSchemaType.KEYWORD,
    "professions": PayloadSchemaType.KEYWORD,
    "plan_version": PayloadSchemaType.KEYWORD,
    "is_meaningful": PayloadSchemaType.BOOL,
    "is_general": PayloadSchemaType.BOOL,
}


def _uuid(kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kind}-{key}"))


def _ensure_collection(recreate: bool = False):
    names = [c.name for c in client.get_collections().collections]
    if COLLECTION in names and recreate:
        client.delete_collection(COLLECTION)
        names.remove(COLLECTION)
    if COLLECTION not in names:
        client.create_collection(COLLECTION, vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE))
    for field, schema in _INDEXES.items():
        try:
            client.create_payload_index(COLLECTION, field_name=field, field_schema=schema)
        except Exception:
            pass


def _payload(level, doc_id, section_id, chunk_id, sec, label) -> dict:
    subs = label.get("substages") or []
    sub_ids = [s.get("id") if isinstance(s, dict) else s for s in subs]
    confs = [s.get("confidence", 1.0) for s in subs if isinstance(s, dict)]
    return {
        "doc_id": doc_id, "section_id": section_id, "chunk_id": chunk_id, "level": level,
        "is_meaningful": bool(label.get("is_meaningful", True)),
        "substages": sub_ids,
        "substage_conf": max(confs) if confs else None,
        "stages": list(label.get("stages") or []),
        "professions": list(label.get("professions") or []),
        "is_general": bool(label.get("is_general", False)),
        "plan_version": label.get("plan_version"),
        "heading_path": list(sec.get("heading_path") or []),
        "page_from": sec.get("page_from"),
        "text": sec.get("text") or "",
    }


def reindex(batch: int = 128) -> dict:
    """Полная пересборка Qdrant из PG. Без LLM: только эмбеддинги секций/чанков и запись.
    Метки чанков наследуются от секции (в PG у чанков меток нет)."""
    _ensure_collection(recreate=True)
    points, n_sec, n_chunk = [], 0, 0

    def flush():
        if points:
            client.upsert(COLLECTION, points=points)
            points.clear()

    for row in store.iter_labeled_sections():
        sec = {"heading_path": row["heading_path"], "page_from": row["page_from"], "text": row["text"]}
        label = {k: row[k] for k in ("is_meaningful", "substages", "stages", "professions", "is_general", "plan_version")}
        sid = row["section_id"]
        points.append(PointStruct(id=_uuid("section", sid), vector=get_embedding(row["text"]),
                                  payload=_payload("section", row["doc_id"], sid, None, sec, label)))
        n_sec += 1
        for ch in store.list_chunks(sid):
            csec = dict(sec); csec["text"] = ch["text"]
            points.append(PointStruct(id=_uuid("chunk", ch["id"]), vector=get_embedding(ch["text"]),
                                      payload=_payload("chunk", row["doc_id"], sid, ch["id"], csec, label)))
            n_chunk += 1
        if len(points) >= batch:
            flush()
    flush()
    return {"sections": n_sec, "chunks": n_chunk}


def sink_document(doc_id: str) -> dict:
    """Записывает секции и чанки ОДНОГО документа в Qdrant (инкрементально, при ingest).
    Метки берутся из PG (section_labels), у чанков наследуются от секции."""
    _ensure_collection(recreate=False)
    # снять прежние точки документа
    client.delete(COLLECTION, points_selector=Filter(
        must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]))
    points, n_sec, n_chunk = [], 0, 0
    for row in store.iter_labeled_sections():
        if row["doc_id"] != doc_id:
            continue
        sec = {"heading_path": row["heading_path"], "page_from": row["page_from"], "text": row["text"]}
        label = {k: row[k] for k in ("is_meaningful", "substages", "stages", "professions", "is_general", "plan_version")}
        sid = row["section_id"]
        points.append(PointStruct(id=_uuid("section", sid), vector=get_embedding(row["text"]),
                                  payload=_payload("section", doc_id, sid, None, sec, label)))
        n_sec += 1
        for ch in store.list_chunks(sid):
            csec = dict(sec); csec["text"] = ch["text"]
            points.append(PointStruct(id=_uuid("chunk", ch["id"]), vector=get_embedding(ch["text"]),
                                      payload=_payload("chunk", doc_id, sid, ch["id"], csec, label)))
            n_chunk += 1
    if points:
        client.upsert(COLLECTION, points=points)
    return {"sections": n_sec, "chunks": n_chunk}


def retrieve(substage_id: str, position: str = "", plan_version: str = "current",
             limit: int = 8) -> list:
    """Выборка СЕКЦИЙ под подэтап и должность: filter substage AND (profession=position OR
    is_general), мягкий приоритет своей профессии (×1.2). При < 3 результатах — добор общими."""
    def _search(must):
        vec = get_embedding(substage_id)   # запрос — по id подэтапа; вызывающий может передать текст
        return client.query_points(COLLECTION, query=vec, limit=max(limit * 2, limit),
                                   with_payload=True, query_filter=Filter(must=must)).points

    base = [
        FieldCondition(key="level", match=MatchValue(value="section")),
        FieldCondition(key="is_meaningful", match=MatchValue(value=True)),
        FieldCondition(key="substages", match=MatchValue(value=substage_id)),
        FieldCondition(key="plan_version", match=MatchValue(value=plan_version)),
    ]
    prof_or_general = Filter(should=[
        FieldCondition(key="professions", match=MatchValue(value=position)) if position else
        FieldCondition(key="is_general", match=MatchValue(value=True)),
        FieldCondition(key="is_general", match=MatchValue(value=True)),
    ])
    hits = _search(base + [prof_or_general])
    if len(hits) < RETRIEVE_MIN:      # добор общими
        hits = _search(base + [FieldCondition(key="is_general", match=MatchValue(value=True))])

    def score(h):
        pl = h.payload or {}
        boost = PROF_BOOST if position and position in (pl.get("professions") or []) else 1.0
        return h.score * boost

    hits = sorted(hits, key=score, reverse=True)[:limit]
    return [{
        "section_id": (h.payload or {}).get("section_id"),
        "doc_id": (h.payload or {}).get("doc_id"),
        "heading_path": (h.payload or {}).get("heading_path") or [],
        "professions": (h.payload or {}).get("professions") or [],
        "is_general": (h.payload or {}).get("is_general"),
        "text": (h.payload or {}).get("text") or "",
        "score": round(score(h), 4),
    } for h in hits]
