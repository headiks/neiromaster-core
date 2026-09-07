"""
Просмотр индексации документа для админки — только ЧТЕНИЕ из Qdrant.

Три экрана: как документ разбит на чанки и как выглядит вектор каждого
(get_document_chunks), к каким подэтапам каталога отнесён каждый чанк и насколько
уверенно — с обоснованием по косинусу (document_substage_map), и что лежит в
смысловой папке (get_folder_chunks).

Отделено от indexing.py: здесь нет ни записи в Qdrant, ни пайплайна docling —
только выборки для UI. Общий низкоуровневый слой (клиент Qdrant, имя коллекции,
векторы этапов/подэтапов) берём из indexing, чтобы клиент и кэш были в одном
экземпляре. Зависимость строго односторонняя: docview -> indexing.
"""

import math
from typing import Optional

from qdrant_client.models import Filter, FieldCondition, MatchValue

from config import cosine
from indexing import (client, COLLECTION_NAME, PLAN_STAGE_MATCH,
                      _select_substages, _catalog_stage_vectors)


def _vector_stats(vector) -> dict:
    """Компактная сводка по вектору чанка: размерность, норма, первые значения.
    Полный вектор из 1024 чисел в UI не нужен — для инспекции достаточно превью."""
    vec = list(vector or [])
    dim = len(vec)
    norm = math.sqrt(sum(v * v for v in vec)) if vec else 0.0
    return {
        "dim": dim,
        "norm": round(norm, 4),
        "preview": [round(float(v), 4) for v in vec[:16]],
    }


def get_document_chunks(filename: str) -> Optional[dict]:
    """
    Подробности индексации одного документа для админки: как он разбит на чанки
    и как выглядит вектор каждого чанка. None — если чанков в Qdrant нет.
    """
    points = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=filename))]),
            limit=256,
            with_payload=True,
            with_vectors=True,
            offset=offset,
        )
        points.extend(batch)
        if offset is None:
            break

    if not points:
        return None

    chunks = []
    for p in points:
        payload = p.payload or {}
        chunks.append({
            "id": str(p.id),
            "chunk_index": payload.get("chunk_index"),
            "section": payload.get("section"),
            "headings": payload.get("headings") or [],
            "page": payload.get("page"),
            "meaningful": payload.get("meaningful", True),
            "folders": payload.get("folders") or [],
            "stage_ids": payload.get("stage_ids") or [],
            "plan_stages": payload.get("plan_stages") or [],
            "plan_substages": payload.get("plan_substages") or [],
            "profession": payload.get("profession") or "",
            "length": payload.get("length"),
            "text": payload.get("text", ""),          # то, что реально ушло в эмбеддинг
            "raw_text": payload.get("raw_text", ""),   # исходный текст пункта без контекста заголовков
            "vector": _vector_stats(p.vector),
        })
    chunks.sort(key=lambda c: (c["chunk_index"] is None, c["chunk_index"] or 0))
    return {"filename": filename, "chunks": chunks, "count": len(chunks)}


def document_substage_map(filename: str) -> Optional[dict]:
    """Разбивка документа по подэтапам с ОБОСНОВАНИЕМ: для каждого содержательного чанка —
    к каким подэтапам он отнесён и НАСКОЛЬКО близок (косинус к «запросу подэтапа» каталога).
    Score — и есть критерий: чем выше, тем увереннее; низкий у пункта → вероятно, попал ошибочно.
    Возвращает {filename, chunks:[{chunk_index, section, page, text, meaningful,
    matches:[{substage_id, stage_id, stage_title, title, brief, score}]}]}."""
    import planner
    cat = planner.load_catalog()
    meta = {}   # sub_id -> подписи для UI
    for st in cat.get("stages") or []:
        for sub in st.get("substage_templates") or []:
            meta[sub["id"]] = {"stage_id": st["id"], "stage_title": st.get("title", ""),
                               "title": sub.get("title", ""), "brief": sub.get("brief", "")}
    _, sub_vecs = _catalog_stage_vectors()   # [(stage_id, sub_id, vec)]

    points = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=filename))]),
            limit=256, with_payload=True, with_vectors=True, offset=offset,
        )
        points.extend(batch)
        if offset is None:
            break
    if not points:
        return None

    chunks = []
    for p in points:
        pl = p.payload or {}
        meaningful = pl.get("meaningful", True)
        matches = []
        if meaningful:
            scored = [(stid, sub_id, cosine(p.vector, sv)) for stid, sub_id, sv in sub_vecs]
            accepted = {(st, sub) for st, sub, _ in _select_substages(scored)}   # реальные привязки
            for stid, sub_id, sc in scored:
                if sc >= PLAN_STAGE_MATCH:   # показываем и кандидатов — для ручной оценки
                    m = meta.get(sub_id, {})
                    matches.append({"substage_id": sub_id, "stage_id": stid,
                                    "stage_title": m.get("stage_title", ""), "title": m.get("title", ""),
                                    "brief": m.get("brief", ""), "score": round(sc, 3),
                                    "accepted": (stid, sub_id) in accepted})
            matches.sort(key=lambda x: (x["accepted"], x["score"]), reverse=True)
        chunks.append({
            "chunk_index": pl.get("chunk_index"),
            "section": pl.get("section") or "",
            "page": pl.get("page"),
            "meaningful": meaningful,
            "text": pl.get("raw_text") or pl.get("text") or "",
            "matches": matches,
        })
    chunks.sort(key=lambda c: (c["chunk_index"] is None, c["chunk_index"] or 0))
    return {"filename": filename, "threshold": PLAN_STAGE_MATCH, "chunks": chunks, "count": len(chunks)}


def get_folder_chunks(slug: str, limit: int = 1000) -> dict:
    """Чанки, отнесённые к смысловой папке (payload.folders содержит slug) — для просмотра
    содержимого папки в админке. Векторы не тянем (для просмотра не нужны, легче ответ).
    Отсортированы по документу и порядку чанка. limit — верхний предел (ponytail: для
    браузинга хватает; при очень больших папках покажем первые N и count=предел)."""
    points = []
    offset = None
    while len(points) < limit:
        batch, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="folders", match=MatchValue(value=slug))]),
            limit=min(256, limit - len(points)),
            with_payload=True, with_vectors=False, offset=offset,
        )
        points.extend(batch)
        if offset is None:
            break

    chunks = []
    for p in points:
        payload = p.payload or {}
        chunks.append({
            "id": str(p.id),
            "source": payload.get("source"),
            "chunk_index": payload.get("chunk_index"),
            "section": payload.get("section"),
            "page": payload.get("page"),
            "length": payload.get("length"),
            "folders": payload.get("folders") or [],
            "text": payload.get("text", ""),
        })
    chunks.sort(key=lambda c: ((c["source"] or ""), c["chunk_index"] is None, c["chunk_index"] or 0))
    return {"slug": slug, "chunks": chunks, "count": len(chunks), "truncated": len(points) >= limit}
