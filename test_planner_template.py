"""Проверки универсального шаблона плана и «запроса этапа» для раскладки чанков по этапам
(чистая логика над каталогом data/stage_catalog.json — без сети/БД)."""

import planner


def test_full_template_covers_catalog():
    cat = planner.load_catalog()
    plan = planner.build_full_template("Тест")
    # все этапы каталога и все их подэтапы попали в шаблон, порядок сохранён
    assert len(plan["stages"]) == len(cat["stages"])
    for pst, cst in zip(plan["stages"], cat["stages"]):
        assert pst["catalog_id"] == cst["id"]
        assert pst["title"] == cst["title"]
        assert pst["anchor"] == cst.get("anchor", "from_start")
        assert len(pst["substages"]) == len(cst.get("substage_templates") or [])
        for psub, csub in zip(pst["substages"], cst["substage_templates"]):
            assert psub["catalog_id"] == csub["id"]
            assert psub["kind"] == csub.get("kind", "message")
            assert psub["brief"] == csub.get("brief", "")
            assert psub["source"] == "template"
    # редактируемость: это обычный нормализованный план с id и schema_version
    assert plan.get("plan_id") and plan.get("schema_version")


def test_catalog_stage_query_nonempty():
    for st in planner.load_catalog()["stages"]:
        q = planner.catalog_stage_query(st)
        assert st["title"] in q and len(q) > len(st["title"])   # заголовок + брифы подэтапов


def test_catalog_substage_query_nonempty():
    st = planner.load_catalog()["stages"][0]
    sub = (st.get("substage_templates") or [])[0]
    q = planner.catalog_substage_query(st, sub)
    assert sub["title"] in q and st["title"] in q     # контекст этапа + сам подэтап


def test_profession_slug_stable():
    assert planner.profession_slug("Водитель автомобиля") == planner.profession_slug("Водитель автомобиля")
    assert planner.profession_slug("") == "obshiy"
    assert planner.profession_slug("Электрогазосварщик 5р") == planner.profession_slug("Электрогазосварщик 5р")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK ", name)
    print("test_planner_template: все проверки пройдены")
