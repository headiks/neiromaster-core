"""Проверки приоритета поиска по должности сотрудника (чистая логика, без сети/Qdrant):
косинус, правило поправки по близости профессии и её нейтральность для общих документов.

Запуск:  pytest test_profession.py   ИЛИ   python test_profession.py
"""

import sys
import types
from unittest.mock import MagicMock

# ---- заглушки тяжёлых зависимостей ДО импорта модулей проекта ----
for _n in ("psycopg", "psycopg.types", "psycopg.rows", "psycopg_pool",
           "qdrant_client", "qdrant_client.models"):
    sys.modules.setdefault(_n, types.ModuleType(_n))
sys.modules["psycopg.types.json"] = types.ModuleType("psycopg.types.json")
sys.modules["psycopg.types.json"].Json = lambda x: x
sys.modules["qdrant_client"].QdrantClient = lambda *a, **k: MagicMock()
for _n in ["VectorParams", "Distance", "PointStruct", "Filter", "FieldCondition",
           "MatchValue", "MatchAny", "PayloadSchemaType"]:
    setattr(sys.modules["qdrant_client.models"], _n, MagicMock())
_db = types.ModuleType("db"); _db.query = _db.execute = lambda *a, **k: None
sys.modules.setdefault("db", _db)
sys.modules.setdefault("requests", MagicMock())
_cfg = types.ModuleType("config")
_cfg.OLLAMA_URL = "http://x"; _cfg.QDRANT_HOST = "x"; _cfg.QDRANT_PORT = 0
_cfg.get_embedding = lambda t: [0.0]
sys.modules.setdefault("config", _cfg)
sys.modules.setdefault("classify", MagicMock())
sys.modules.setdefault("folders", MagicMock())

import rag


def test_cos():
    assert abs(rag._cos([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(rag._cos([1, 0], [0, 1]) - 0.0) < 1e-9
    assert rag._cos([], [1]) == 0.0            # пустой вектор — безопасный 0


def test_delta_rule():
    # выше порога совпадения -> бонус; ниже порога несовпадения -> штраф; между -> 0
    assert rag._prof_delta_from_sim(0.9) == rag.PROF_BONUS
    assert rag._prof_delta_from_sim(rag.PROF_MATCH_SIM) == rag.PROF_BONUS
    assert rag._prof_delta_from_sim(0.1) == -rag.PROF_PENALTY
    mid = (rag.PROF_MATCH_SIM + rag.PROF_MISMATCH_SIM) / 2
    assert rag._prof_delta_from_sim(mid) == 0.0


def test_profession_delta_neutral_and_active():
    # общий документ (profession="") или нет должности -> нейтрально
    assert rag.profession_delta("", [1, 0]) == 0.0
    assert rag.profession_delta("водитель", None) == 0.0
    # совпадение/несовпадение профессии с вектором должности через подменённый _prof_vec
    rag._prof_vec_cache.clear()
    rag._prof_vec_cache["водитель"] = [1.0, 0.0]     # «совпадает» с должностью-вектором ниже
    rag._prof_vec_cache["сварщик"] = [0.0, 1.0]      # ортогонален -> чужая профессия
    pos_vec = [1.0, 0.0]                              # должность сотрудника ~ водитель
    assert rag.profession_delta("водитель", pos_vec) == rag.PROF_BONUS
    assert rag.profession_delta("сварщик", pos_vec) == -rag.PROF_PENALTY


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK ", name)
    print("test_profession: все проверки пройдены")
