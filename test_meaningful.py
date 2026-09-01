"""Проверка фильтра смысловой нагрузки чанка (classify.is_meaningful) — чистая логика,
без сети/Qdrant. Мусор (заголовки, номера страниц, оглавление) не должен распределяться."""

import sys
import types
from unittest.mock import MagicMock

for _n in ("qdrant_client", "qdrant_client.models", "folders", "config"):
    sys.modules.setdefault(_n, types.ModuleType(_n))
sys.modules["qdrant_client"].QdrantClient = lambda *a, **k: MagicMock()
for _n in ["VectorParams", "Distance", "PointStruct", "Filter", "FieldCondition", "MatchValue"]:
    setattr(sys.modules["qdrant_client.models"], _n, MagicMock())
cfg = sys.modules["config"]
cfg.QDRANT_HOST = "x"; cfg.QDRANT_PORT = 0; cfg.EMBED_DIM = 8; cfg.OLLAMA_URL = "http://x"
cfg.get_embedding = lambda t: [0.0] * 8
sys.modules.setdefault("requests", MagicMock())

import classify


def test_junk_is_not_meaningful():
    for junk in ["", "12", "  ", "СИЗ", "Стр. 5", "- 14 -", "………… 42",
                 "Раздел 3 ....... 12", "1.2.3", "N°", "Оглавление"]:
        assert classify.is_meaningful(junk) is False, junk


def test_real_text_is_meaningful():
    for good in [
        "Работник обязан пройти предрейсовый медицинский осмотр перед началом смены.",
        "Скорость на территории предприятия не более 5 км/ч, на поворотах 3 км/ч.",
        "Отпуск предоставляется 28 календарных дней в год.",
    ]:
        assert classify.is_meaningful(good) is True, good


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK ", name)
    print("test_meaningful: все проверки пройдены")
