"""Проверка детерминированного ЧС-детектора (ТЗ 1.7) — чистые регэкспы, без сети.
Тяжёлые зависимости rag.py (qdrant/requests/config/folders/classify) подменяются заглушками."""

import test_stubs

test_stubs.install(embed_dim=8)   # qdrant/requests/config — заглушки, см. test_stubs.py

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
