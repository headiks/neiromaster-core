"""
Тесты пайплайна разметки docpipe — чистая логика без сети/БД/Qdrant (тяжёлые зависимости
заглушены). Покрывает: валидацию JSON модели, префильтр мусора, наследование меток,
идемпотентность по content_hash, сегментацию/чанкинг.

Запуск:  python test_docpipe.py   ИЛИ   pytest test_docpipe.py
"""

import sys
import types
from unittest.mock import MagicMock

# ---- заглушки тяжёлых зависимостей ДО импорта пакета docpipe ----
for _n in ("psycopg", "psycopg.types", "psycopg.rows", "psycopg_pool",
           "qdrant_client", "qdrant_client.models", "requests"):
    sys.modules.setdefault(_n, types.ModuleType(_n))
sys.modules["psycopg.types.json"] = types.ModuleType("psycopg.types.json")
sys.modules["psycopg.types.json"].Json = lambda x: x
sys.modules["qdrant_client"].QdrantClient = lambda *a, **k: MagicMock()
for _n in ["VectorParams", "Distance", "PointStruct", "Filter", "FieldCondition",
           "MatchValue", "MatchAny", "PayloadSchemaType"]:
    setattr(sys.modules["qdrant_client.models"], _n, MagicMock())

# управляемый стаб БД
_db = types.ModuleType("db")
_db._exec_log = []
_db._query_hook = lambda sql, params=(), fetch="all": None
_db.execute = lambda sql, params=(): _db._exec_log.append((sql, params))
_db.query = lambda sql, params=(), fetch="all": _db._query_hook(sql, params, fetch)
sys.modules["db"] = _db

_cfg = types.ModuleType("config")
_cfg.QDRANT_HOST = "x"; _cfg.QDRANT_PORT = 0; _cfg.EMBED_DIM = 4; _cfg.OLLAMA_URL = "http://x"
_cfg.get_embedding = lambda t: [1.0, 0.0, 0.0, 0.0]
sys.modules["config"] = _cfg

from docpipe import core, store, professions


STRUCTURE = {"stages": [
    {"id": "firstday", "title": "Первый день", "substages": [
        {"id": "firstday.equipment", "title": "СИЗ", "description": "выдача СИЗ"},
        {"id": "firstday.rules", "title": "Правила", "description": "распорядок"}]},
    {"id": "training", "title": "Стажировка", "substages": [
        {"id": "training.shift", "title": "Смена", "description": "работа под наставником"}]},
]}


# ---------- Префильтр мусора ----------
def test_prefilter_junk():
    for junk, reason in [("", "empty"), ("5", "clause_number"), ("5.1.2.", "clause_number"),
                         ("12", "clause_number"), ("Стр. 7", "page_number"),
                         ("Раздел 3 ....... 12", "toc_leader"), ("СИЗ", "low_content")]:
        ok, r = core.prefilter(junk)
        assert ok is False, junk
        assert r == reason, (junk, r)


def test_prefilter_real():
    ok, r = core.prefilter("Работник обязан пройти предрейсовый медицинский осмотр перед сменой.")
    assert ok is True and r is None


def test_repeated_lines_headers():
    pages = [["ООО Компания", "текст один"], ["ООО Компания", "текст два"],
             ["ООО Компания", "текст три"], ["иное", "текст"]]
    rep = core.repeated_lines(pages, threshold=0.6)
    assert "ООО Компания" in rep and "текст один" not in rep


# ---------- Валидация/нормализация JSON модели (проход 2) ----------
def test_coerce_drops_unknown_and_coerces_conf():
    raw = {"is_meaningful": True,
           "substages": [{"id": "firstday.equipment", "confidence": "0.8"},
                         {"id": "NETU", "confidence": 0.9},          # нет в плане -> отбросить
                         {"id": "training.shift", "confidence": 5}], # >1 -> clamp
           "professions": ["водитель"], "is_general": False, "why": "x"}
    out = core.coerce_section_labels(raw, STRUCTURE)
    ids = [s["id"] for s in out["substages"]]
    assert ids == ["firstday.equipment", "training.shift"]           # NETU выкинут
    assert out["substages"][0]["confidence"] == 0.8
    assert out["substages"][1]["confidence"] == 1.0                  # clamp
    assert set(out["stages"]) == {"firstday", "training"}            # этапы из подэтапов
    assert out["is_general"] is False and out["professions"] == ["водитель"]


def test_coerce_general_empty():
    out = core.coerce_section_labels({"is_meaningful": True, "substages": [], "professions": [],
                                      "is_general": True, "why": ""}, STRUCTURE)
    assert out["substages"] == [] and out["stages"] == [] and out["is_general"] is True


def test_stages_from_substages():
    assert core.stages_from_substages(["firstday.equipment", "training.shift"], STRUCTURE) == ["firstday", "training"]
    assert core.valid_substage_ids(STRUCTURE) == {"firstday.equipment", "firstday.rules", "training.shift"}


# ---------- Наследование меток секции в чанк ----------
def test_inherit_labels():
    sec = {"is_meaningful": True, "substages": [{"id": "training.shift", "confidence": 0.7}],
           "stages": ["training"], "professions": ["водитель"], "is_general": False}
    ch = core.inherit_labels(sec)
    assert ch["substages"] == [{"id": "training.shift", "confidence": 0.7}]
    assert ch["stages"] == ["training"] and ch["professions"] == ["водитель"]
    assert ch["source"] == "inherited"
    ch["stages"].append("x")                       # копия, не ссылка
    assert sec["stages"] == ["training"]


# ---------- Сегментация и мелкий чанкинг ----------
def test_split_section_small_whole():
    assert core.split_section_text("Короткий текст из нескольких слов тут.", 1200) == \
        ["Короткий текст из нескольких слов тут."]


def test_to_chunks_by_sentences():
    text = " ".join(f"Предложение номер {i} с достаточной длиной для набора токенов." for i in range(40))
    chunks = core.to_chunks(text, min_tokens=20, max_tokens=40, overlap_sentences=1)
    assert len(chunks) >= 2
    assert all(c.strip() for c in chunks)


# ---------- Матч профессий со штаткой ----------
def test_professions_match_exact_and_embed():
    emb = {"водитель": [1, 0], "Водитель автомобиля": [0.99, 0.14], "сварщик": [0, 1]}
    matched, conf = professions.match_to_staffing(
        ["водитель"], ["Водитель автомобиля", "Сварщик"],
        embed=lambda t: emb.get(t, [0, 0]))
    assert matched == ["Водитель автомобиля"] and conf and conf >= 0.7
    # ниже порога -> отбрасываем
    matched2, _ = professions.match_to_staffing(
        ["бухгалтер"], ["Водитель автомобиля"], embed=lambda t: {"бухгалтер": [0, 1], "Водитель автомобиля": [1, 0]}.get(t, [0, 0]))
    assert matched2 == []


# ---------- Идемпотентность по content_hash ----------
def test_upsert_document_idempotent():
    _db._exec_log.clear()
    _db._query_hook = lambda sql, params=(), fetch="all": {"id": "doc1"}   # hash уже есть
    doc_id, changed = store.upsert_document("f.pdf", "HASH", {}, "docling", "qwen3:14b")
    assert doc_id == "doc1" and changed is False
    assert _db._exec_log == []                      # ничего не вставили

    _db._query_hook = lambda sql, params=(), fetch="all": None            # нового hash нет
    doc_id2, changed2 = store.upsert_document("f.pdf", "HASH2", {}, "docling", "qwen3:14b")
    assert changed2 is True and doc_id2
    assert any("INSERT INTO documents" in sql for sql, _ in _db._exec_log)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK ", name)
    print("test_docpipe: все проверки пройдены")
