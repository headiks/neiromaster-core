"""
Самопроверка чистой логики реестра документов (Вариант 1): косинус, привязка к
подэтапам по вектору, сборка данных экрана. Без Postgres/Ollama/Qdrant.
Запуск: python3 test_documents.py
"""
import documents as d


def test_cosine():
    assert d.cosine([1, 0], [1, 0]) == 1.0
    assert d.cosine([1, 0], [0, 1]) == 0.0
    assert round(d.cosine([1, 1], [1, 0]), 3) == 0.707
    assert d.cosine([], [1]) == 0.0          # пустой/разной длины -> 0
    assert d.cosine([0, 0], [1, 1]) == 0.0   # нулевой -> 0


def test_assign_substages():
    # Документ ближе всего к «ТБ» (вектор [1,0,0]); порог отсекает слабые.
    doc = [1.0, 0.0, 0.0]
    subs = [
        {"stage_id": "s1", "substage_id": "tb",   "title": "Охрана труда",  "vec": [0.9, 0.1, 0.0]},
        {"stage_id": "s1", "substage_id": "work", "title": "Рабочее место", "vec": [0.0, 1.0, 0.0]},
        {"stage_id": "s2", "substage_id": "att",  "title": "Аттестация",    "vec": [0.8, 0.0, 0.2]},
    ]
    res = d.assign_substages(doc, subs, threshold=0.35)
    by_stage = {r["stage_id"]: r for r in res}
    # В этапе s1 победил «Охрана труда», «Рабочее место» (косинус 0) отсеян
    assert by_stage["s1"]["substage_id"] == "tb"
    # В этапе s2 прошла «Аттестация»
    assert by_stage["s2"]["substage_id"] == "att"
    # Результат отсортирован по score убыв.
    assert res[0]["score"] >= res[-1]["score"]

    # Высокий порог -> ничего не проходит
    assert d.assign_substages(doc, subs, threshold=0.999) == []


def test_build_board():
    stages = [
        {"id": "s1", "title": "Первая неделя", "description": "погружение",
         "substages": [{"id": "tb", "title": "Охрана труда"}, {"id": "work", "title": "Рабочее место"}]},
        {"id": "s2", "title": "Первый месяц", "description": "самостоятельно",
         "substages": [{"id": "att", "title": "Аттестация"}]},
    ]
    docs = [
        # привязан к подэтапу tb этапа s1
        {"sha256": "h1", "filename": "ТБ.pdf", "mime": "pdf", "stage_ids": ["s1"],
         "substages": [{"stage_id": "s1", "substage_id": "tb", "score": 0.9}]},
        # отнесён к этапу s2, но без подэтапа
        {"sha256": "h2", "filename": "Приказ.pdf", "mime": "pdf", "stage_ids": ["s2"], "substages": []},
        # ни к чему -> unassigned
        {"sha256": "h3", "filename": "Этика.pdf", "mime": "pdf", "stage_ids": [], "substages": []},
    ]
    board = d.build_board(stages, docs)

    s1 = next(s for s in board["stages"] if s["id"] == "s1")
    tb = next(x for x in s1["substages"] if x["id"] == "tb")
    assert [doc["sha256"] for doc in tb["documents"]] == ["h1"]

    s2 = next(s for s in board["stages"] if s["id"] == "s2")
    assert [doc["sha256"] for doc in s2["documents"]] == ["h2"]   # в этапе, без подэтапа

    assert [doc["sha256"] for doc in board["unassigned"]] == ["h3"]
    assert board["stats"] == {"stages": 2, "substages": 3, "documents": 3, "unassigned": 1}


def test_keywords():
    kw = d.extract_keywords("Инструктаж по технике безопасности. Техника безопасности и спецодежда.", n=3)
    assert "безопасности" in kw          # самое частое значимое слово
    assert "по" not in kw and "и" not in kw   # стоп-слова отброшены


if __name__ == "__main__":
    test_cosine()
    test_assign_substages()
    test_build_board()
    test_keywords()
    print("OK: реестр документов (Вариант 1) — чистая логика работает")
