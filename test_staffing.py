"""Тесты штатки: логины + универсальный разбор (классификатор ячеек подменяем стабом,
без сети/БД)."""

import sys
import types
from unittest.mock import MagicMock

# ---- заглушки тяжёлых зависимостей ДО импорта staffing (users -> db -> psycopg_pool) ----
for _n in ("psycopg", "psycopg.types", "psycopg.rows", "psycopg_pool"):
    sys.modules.setdefault(_n, types.ModuleType(_n))
sys.modules["psycopg.types.json"] = types.ModuleType("psycopg.types.json")
sys.modules["psycopg.types.json"].Json = lambda x: x
sys.modules.setdefault("db", types.ModuleType("db"))
sys.modules.setdefault("users", MagicMock())
sys.modules.setdefault("requests", MagicMock())

import staffing


def test_username_base():
    assert staffing.username_base("Иванов Иван Иванович") == "ivanov_i_i"
    assert staffing.username_base("Петров Пётр") == "petrov_p"
    assert staffing.username_base("") == "user"
    assert staffing.username_base("Ли") == "li0"
    assert staffing.username_base("Smith John") == "smith_j"


def test_unique_username_collision():
    taken = {"ivanov_i_i", "ivanov_i_i2"}
    assert staffing._unique_username("ivanov_i_i", taken) == "ivanov_i_i3"


def test_parse_labels_ignores_trailing():
    assert staffing._parse_labels('["person","org","position"] и ещё текст', 3) == ["person", "org", "position"]
    assert staffing._parse_labels('["person","weird"]', 2) == ["person", "other"]   # чужой класс -> other


def _stub(mapping):
    # батч-классификатор: chunk -> {text: class}
    return lambda chunk: {t: mapping.get(t, "other") for t in chunk}


def test_extract_roster_with_banners():
    grid = [
        ["№", "Сотрудник", "Должность", "Дата"],
        ["Администрация", "", "", ""],
        ["1", "Иванов Иван Иванович", "Бухгалтер", "06.02.2026"],
        ["Отдел продаж", "", "", ""],
        ["2", "", "Менеджер", ""],
        ["Итого", "", "", ""],
    ]
    mapping = {"data_start_row": 1,
               "columns": {"full_name": 1, "position": 2, "department": None, "start_date": 3},
               "sections": {"department": 0}}
    cls = _stub({"Иванов Иван Иванович": "person", "Бухгалтер": "position", "Менеджер": "position",
                 "Администрация": "org", "Отдел продаж": "org", "Итого": "other"})
    recs = staffing.extract_unified(grid, mapping, classifier=cls)
    assert recs == [
        {"full_name": "Иванов Иван Иванович", "position": "Бухгалтер", "department": "Администрация", "start_date": "06.02.2026"},
        {"full_name": "", "position": "Менеджер", "department": "Отдел продаж", "start_date": ""},
    ]


def test_extract_positions_and_banners_one_column():
    grid = [
        ["Должность", "Ставок"],
        ["Обособленное подразделение А", "1"],
        ["Ведущий инженер", "1"],
        ["Кладовщик", "8"],
    ]
    mapping = {"data_start_row": 1,
               "columns": {"full_name": None, "position": 0, "department": None, "start_date": None},
               "sections": {"department": 0}}
    cls = _stub({"Обособленное подразделение А": "org", "Ведущий инженер": "position", "Кладовщик": "position"})
    recs = staffing.extract_unified(grid, mapping, classifier=cls)
    assert [r["position"] for r in recs] == ["Ведущий инженер", "Кладовщик"]
    assert all(r["department"] == "Обособленное подразделение А" and r["full_name"] == "" for r in recs)


def test_extract_shifted_name_in_position_column():
    # ФИО «разъехалось» в колонку должности (объединённые ячейки/сбитый шаблон):
    # маршрут по КЛАССУ ячейки должен всё равно завести человека, а не выкинуть строку.
    grid = [
        ["Сотрудник", "Должность", "Дата"],
        ["Иванов Иван Иванович", "Бухгалтер", "06.02.2026"],
        ["", "Дулгерова Елена Александровна", ""],   # имя стоит в колонке должности
    ]
    mapping = {"data_start_row": 1,
               "columns": {"full_name": 0, "position": 1, "department": None, "start_date": 2},
               "sections": {}}
    cls = _stub({"Иванов Иван Иванович": "person", "Бухгалтер": "position",
                 "Дулгерова Елена Александровна": "person"})
    recs = staffing.extract_unified(grid, mapping, classifier=cls)
    assert recs == [
        {"full_name": "Иванов Иван Иванович", "position": "Бухгалтер", "department": "", "start_date": "06.02.2026"},
        {"full_name": "Дулгерова Елена Александровна", "position": "", "department": "", "start_date": ""},
    ]


def test_extract_recheck_name_in_unclassified_column():
    # ФИО оказалось в колонке, которую модель не назвала ни ФИО, ни должностью:
    # перепроверка выпавшей строки классифицирует все её ячейки и находит человека.
    grid = [
        ["Должность", "Разряд", "ФИО"],
        ["Слесарь", "5", "Петров Пётр Петрович"],
    ]
    mapping = {"data_start_row": 1,
               "columns": {"full_name": None, "position": 0, "department": None, "start_date": None},
               "sections": {}}
    cls = _stub({"Слесарь": "position", "Петров Пётр Петрович": "person", "Разряд": "other"})
    recs = staffing.extract_unified(grid, mapping, classifier=cls)
    assert recs == [{"full_name": "Петров Пётр Петрович", "position": "Слесарь",
                     "department": "", "start_date": ""}]


def test_extract_position_column_mislabeled_as_name():
    # модель отнесла к ФИО столбец с должностями -> маршрут по КЛАССУ ячейки, не по колонке
    grid = [["ФИО", "Комментарий"], ["Главный геолог", "руководит"], ["Обособленное подразделение", ""]]
    mapping = {"data_start_row": 1,
               "columns": {"full_name": 0, "position": 1, "department": None, "start_date": None},
               "sections": {}}
    cls = _stub({"Главный геолог": "position", "руководит": "other", "Обособленное подразделение": "org"})
    recs = staffing.extract_unified(grid, mapping, classifier=cls)
    assert recs == [{"full_name": "", "position": "Главный геолог", "department": "", "start_date": ""}]


def test_looks_like_person_dict():
    # реальные ФИО из словаря -> человек; должности/категории/орг -> нет
    assert staffing.looks_like_person("Иванов Иван Иванович")
    assert staffing.looks_like_person("Петров Пётр Петрович")
    assert staffing.looks_like_person("Иванов И.И.")           # с инициалами
    assert not staffing.looks_like_person("Ведущий инженер")
    assert not staffing.looks_like_person("Слесарь 5 разряда")  # цифра -> не имя
    assert not staffing.looks_like_person("Специалисты")        # одно слово
    assert not staffing.looks_like_person("")


def test_extract_person_via_name_dict_overrides_model():
    # классификатор ошибочно назвал ФИО должностью — словарь ФИО всё равно заводит человека
    grid = [
        ["ФИО", "Должность", "Дата"],
        ["Петров Пётр Петрович", "Слесарь", "01.03.2026"],
    ]
    mapping = {"data_start_row": 1,
               "columns": {"full_name": 0, "position": 1, "department": None, "start_date": 2},
               "sections": {}}
    cls = _stub({"Петров Пётр Петрович": "position", "Слесарь": "position"})  # модель врёт
    recs = staffing.extract_unified(grid, mapping, classifier=cls)
    assert recs == [{"full_name": "Петров Пётр Петрович", "position": "Слесарь",
                     "department": "", "start_date": "01.03.2026"}]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("ALL PASS")
