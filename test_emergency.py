"""Проверка детерминированного ЧС-детектора (ТЗ 1.7) — чистые регэкспы, без сети.
Тяжёлые зависимости rag.py (qdrant/requests/config/folders/classify) подменяются заглушками."""

import sys
import types
from unittest.mock import MagicMock

# --- заглушки, чтобы импортировать rag без установленных qdrant/ollama ---
for _n in ("qdrant_client", "qdrant_client.models", "folders", "classify", "requests"):
    sys.modules.setdefault(_n, types.ModuleType(_n))
sys.modules["qdrant_client"].QdrantClient = lambda *a, **k: MagicMock()
for _n in ["VectorParams", "Distance", "PointStruct", "Filter", "FieldCondition", "MatchValue", "MatchAny"]:
    setattr(sys.modules["qdrant_client.models"], _n, MagicMock())
cfg = types.ModuleType("config")
cfg.OLLAMA_URL = "http://x"; cfg.QDRANT_HOST = "x"; cfg.QDRANT_PORT = 0
cfg.get_embedding = lambda t: [0.0] * 8
sys.modules["config"] = cfg

import rag


def test_emergency_triggers_fire_injury_electric():
    for q, expect in [
        ("на складе пожар, что делать", "пожар"),
        ("человек получил травму руки", "травма/здоровье"),
        ("рабочего ударило током", "электротравма"),
        ("началась эвакуация здания", "эвакуация"),
        ("произошла утечка газа в цеху", "утечка/химия"),
    ]:
        r = rag.detect_emergency(q)
        assert r is not None, f"ЧС не распознана: {q!r}"
        assert r["risk_type"] == expect, f"{q!r}: ждали {expect}, получили {r['risk_type']}"
        assert r["instruction"], "нет инструкции"


def test_normal_questions_are_not_emergency():
    for q in ["сколько дней отпуска положено", "как оформить пропуск", "где посмотреть график смен"]:
        assert rag.detect_emergency(q) is None, f"ложное срабатывание ЧС на {q!r}"


if __name__ == "__main__":
    ok = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("OK ", name); ok += 1
    print(f"test_emergency: пройдено {ok}")
