"""
Проверки новой модели знаний (папки/этапы/классификация) без БД, Qdrant и Ollama:
тяжёлые зависимости подменяются заглушками в sys.modules, тестируется чистая логика
и целостность стартового сида data/knowledge_seed.json.

Запуск:  pytest test_knowledge.py   ИЛИ   python test_knowledge.py
"""

import sys
import json
import types
import pathlib
from unittest.mock import MagicMock

# ---- заглушки тяжёлых зависимостей ДО импорта модулей проекта ----
_psy = types.ModuleType("psycopg"); sys.modules.setdefault("psycopg", _psy)
_pt = types.ModuleType("psycopg.types"); sys.modules.setdefault("psycopg.types", _pt)
_pj = types.ModuleType("psycopg.types.json"); _pj.Json = lambda x: x
sys.modules.setdefault("psycopg.types.json", _pj)
_pr = types.ModuleType("psycopg.rows"); _pr.dict_row = None
sys.modules.setdefault("psycopg.rows", _pr)
sys.modules.setdefault("psycopg_pool", types.ModuleType("psycopg_pool"))

_db = types.ModuleType("db"); _db.query = lambda *a, **k: None; _db.execute = lambda *a, **k: None
sys.modules.setdefault("db", _db)

_qc = types.ModuleType("qdrant_client"); _qc.QdrantClient = lambda *a, **k: MagicMock()
sys.modules.setdefault("qdrant_client", _qc)
_qm = types.ModuleType("qdrant_client.models")
for _n in ["VectorParams", "Distance", "PointStruct", "Filter", "FieldCondition",
           "MatchValue", "MatchAny", "PayloadSchemaType"]:
    setattr(_qm, _n, MagicMock())
sys.modules.setdefault("qdrant_client.models", _qm)
sys.modules.setdefault("requests", MagicMock())

import folders
import stages
import classify


def test_folder_slug_and_clean():
    assert folders.slugify("Охрана труда") == "ohrana_truda"
    assert folders.slugify("Положение о ПВТР (кратко)") == "polozhenie_o_pvtr_kratko"
    assert folders.slugify("   ").startswith("folder_")
    assert folders._clean_list([" a ", "a", "", None, "b"]) == ["a", "b"]


def test_stage_substages_normalize():
    subs = stages._subs([{"id": "a", "title": " X "}, "Y", {"title": ""}, None])
    assert subs[0] == {"id": "a", "title": "X"}
    assert subs[1]["title"] == "Y" and subs[1]["id"].startswith("s")
    assert len(subs) == 2


def test_classify_pure():
    bs = {"a": {"slug": "a", "stage_ids": ["s1", "s2"]},
          "b": {"slug": "b", "stage_ids": ["s2", "s3"]}}
    assert classify._stage_union(["a", "b"], bs) == ["s1", "s2", "s3"]
    assert classify._stage_union([], bs) == []
    assert classify.folder_tag_text(
        {"name": "Охрана труда", "description": "d", "criteria": ["c1", "c2"]}
    ) == "Охрана труда. d. c1. c2"


def test_seed_integrity():
    seed = json.loads((pathlib.Path(__file__).parent / "data" / "knowledge_seed.json")
                      .read_text(encoding="utf-8"))
    assert len(seed["stages"]) == 8       # блоки из «Блоки и модули»
    assert len(seed["folders"]) == 125    # документы из «Сортировка документов»
    # у каждого блока есть модули (подэтапы)
    for s in seed["stages"]:
        assert s["substages"], s["id"]
    # каждая папка имеет критерии и связана хотя бы с одним этапом
    for f in seed["folders"]:
        assert f["criteria"], f["name"]
        assert f["stage_ids"], f["name"]
    stage_ids = {s["id"] for s in seed["stages"]}
    for f in seed["folders"]:
        for sid in f["stage_ids"]:
            assert sid in stage_ids, f"{f['name']} ссылается на несуществующий этап {sid}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK  {name}")
    print("test_knowledge: все проверки пройдены")
