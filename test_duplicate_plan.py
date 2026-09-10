"""
Самопроверка дублирования планов: новый plan_id, скопированная структура,
расписание/тексты не тянутся из оригинала. Планы теперь в БД (таблица plans).
Нужен доступный Postgres (NEIROMASTER_TEST_DSN). Без БД тест пропускается.
Запуск: python test_duplicate_plan.py
"""
import os

import db
import planner

TEST_DSN = (os.environ.get("NEIROMASTER_TEST_DSN")
            or "postgresql://neiromaster:neiromaster@localhost:5432/neiromaster_test")


def run():
    db.configure(TEST_DSN)
    db.init_schema()
    db.execute("DELETE FROM plan_schedules")
    db.execute("DELETE FROM plans")

    src = planner.save_plan(planner.normalize_plan({
        "title": "Адаптация водителя",
        "role": "Водитель",
        "stages": [{
            "title": "Первый день",
            "duration": {"value": 2, "unit": "days"},
            "substages": [{"title": "Инструктаж", "brief": "про технику безопасности",
                           "schedule": {"day": 1, "time": "10:00"}}],
        }],
    }))

    copy = planner.duplicate_plan(src["plan_id"])
    assert copy is not None
    assert copy["plan_id"] != src["plan_id"], "у копии должен быть свой id"
    assert copy["title"] == "Адаптация водителя (копия)"
    # Структура скопирована один в один
    assert copy["stages"][0]["substages"][0]["brief"] == "про технику безопасности"
    assert copy["stages"][0]["duration"] == {"value": 2, "unit": "days"}
    # Оба плана существуют независимо
    assert planner.load_plan(src["plan_id"]) is not None
    assert planner.load_plan(copy["plan_id"]) is not None

    # Явный заголовок для смежной должности
    named = planner.duplicate_plan(src["plan_id"], "Адаптация курьера")
    assert named["title"] == "Адаптация курьера"
    assert named["plan_id"] not in (src["plan_id"], copy["plan_id"])

    assert planner.duplicate_plan("нет-такого") is None

    # delete уносит и расписания (FK ON DELETE CASCADE)
    planner.save_schedule(src["plan_id"], {"messages": []}, "")
    assert planner.load_schedule(src["plan_id"], "") is not None
    assert planner.delete_plan(src["plan_id"]) is True
    assert planner.load_plan(src["plan_id"]) is None
    assert planner.load_schedule(src["plan_id"], "") is None

    print("OK: дублирование планов работает")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        if "connection" in str(e).lower() or "could not" in str(e).lower():
            print(f"SKIP: PostgreSQL недоступен ({TEST_DSN}). {e}")
        else:
            raise
