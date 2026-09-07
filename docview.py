"""
Просмотр индексации документа для админки — только ЧТЕНИЕ из Qdrant.

Три экрана: как документ разбит на чанки и как выглядит вектор каждого
(get_document_chunks), и что лежит в смысловой папке (get_folder_chunks). Привязка к подэтапам
переехала на LLM-разметку docpipe (доска и «Разбор документа»), косинуса тут больше нет.

Отделено от indexing.py: здесь нет ни записи в Qdrant, ни пайплайна docling —
только выборки для UI. Общий низкоуровневый слой (клиент Qdrant, имя коллекции,
векторы этапов/подэтапов) берём из indexing, чтобы клиент и кэш были в одном
экземпляре. Зависимость строго односторонняя: docview -> indexing.
"""

import math
from typing import Optional

from qdrant_client.models import Filter, FieldCondition, MatchValue

from indexing import client, COLLECTION_NAME


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
