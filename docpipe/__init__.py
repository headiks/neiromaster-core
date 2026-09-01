"""
docpipe — двухпроходная разметка внутренних документов относительно плана адаптации
(этапы → подэтапы) + наследование меток в мелкие чанки для RAG.

Слои: schema (DDL PG) · store (CRUD PG, источник правды) · core (чистая логика) ·
llm (Ollama, 2 прохода) · professions (матч со штаткой) · qdrant_sink (производная копия) ·
pipeline (оркестрация, очередь).

Публичный API ниже.
"""

from .schema import init_schema
from .store import save_plan_version, get_plan_structure, get_job
from .pipeline import ingest, reindex, relabel_candidates, retrieve, enqueue

__all__ = [
    "init_schema", "sync_plan_from_catalog", "save_plan_version", "get_plan_structure",
    "get_job", "ingest", "reindex", "relabel_candidates", "retrieve", "enqueue",
]


def sync_plan_from_catalog(version_id: str = "current") -> dict:
    """Заводит версию плана из каталога этапов (planner). Каталожные подэтапы -> подэтапы
    с описанием (brief). Относительно этой структуры идёт разметка (проход 2)."""
    import planner
    cat = planner.load_catalog()
    structure = {"stages": [{
        "id": st["id"], "title": st.get("title", ""), "description": st.get("description", ""),
        "substages": [{"id": f"{st['id']}.{sub['id']}", "title": sub.get("title", ""),
                       "description": sub.get("brief", "")}
                      for sub in st.get("substage_templates") or []],
    } for st in cat.get("stages") or []]}
    save_plan_version(structure, version_id)
    return structure
