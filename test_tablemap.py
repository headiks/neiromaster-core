"""Тест переиспользуемого маппинга таблицы. Извлечение записей — чистая логика (без ИИ/сети).
map_columns тестируем с подменённым _llm (без Ollama)."""

import tablemap


GRID = [
    ["Штатное расписание", "", ""],
    ["№", "ФИО", "Должность"],
    ["1", "Иванов Иван Иванович", "Водитель"],
    ["2", "Петров Пётр", "Слесарь"],
    ["", "", ""],                     # пустая строка — пропускается
    ["3", "", "Сварщик"],             # нет ФИО — пропускается (required=full_name)
]
FIELDS = [{"name": "full_name", "description": "ФИО"}, {"name": "position", "description": "Должность"}]


def test_extract_skips_header_and_incomplete():
    mapping = {"data_start_row": 2, "columns": {"full_name": 1, "position": 2}}
    recs = tablemap.extract_records(GRID, mapping, required=["full_name"])
    assert recs == [
        {"full_name": "Иванов Иван Иванович", "position": "Водитель"},
        {"full_name": "Петров Пётр", "position": "Слесарь"},
    ]


def test_extract_null_column_gives_empty_field():
    mapping = {"data_start_row": 2, "columns": {"full_name": 1, "position": None}}
    recs = tablemap.extract_records(GRID, mapping, required=["full_name"])
    assert recs[0] == {"full_name": "Иванов Иван Иванович", "position": ""}


def test_extract_out_of_range_col_safe():
    mapping = {"data_start_row": 2, "columns": {"full_name": 1, "x": 99}}
    recs = tablemap.extract_records([["", "A"]], mapping, required=["full_name"])
    # data_start за пределами -> пусто, без исключения
    assert recs == []


def test_map_columns_parses_llm():
    tablemap._llm = lambda s, u: '{"data_start_row": 2, "columns": {"full_name": 1, "position": 2}}'
    m = tablemap.map_columns(GRID, FIELDS)
    assert m == {"data_start_row": 2, "columns": {"full_name": 1, "position": 2}, "sections": {}}


def test_map_columns_coerces_and_filters():
    # строковый индекс -> int; лишнее поле из ответа отбрасывается; отсутствующее -> None
    tablemap._llm = lambda s, u: '{"data_start_row": "2", "columns": {"full_name": "1", "junk": 5}}'
    m = tablemap.map_columns(GRID, FIELDS)
    assert m == {"data_start_row": 2, "columns": {"full_name": 1, "position": None}, "sections": {}}


def test_extract_section_forward_fill():
    # отдел задан строками-баннерами (col0), протягивается вниз до следующего баннера
    grid = [
        ["№", "ФИО", "Должность"],        # заголовок (до data_start)
        ["Администрация", "", ""],         # баннер -> отдел
        ["1", "Иванов", "Бухгалтер"],
        ["2", "Петров", "Кассир"],
        ["Продажи", "", ""],               # следующий баннер
        ["3", "Сидоров", "Менеджер"],
    ]
    mapping = {"data_start_row": 1, "columns": {"full_name": 1, "position": 2, "department": None},
               "sections": {"department": 0}}
    recs = tablemap.extract_records(grid, mapping, required=["full_name"])
    assert [r["full_name"] for r in recs] == ["Иванов", "Петров", "Сидоров"]
    assert [r["department"] for r in recs] == ["Администрация", "Администрация", "Продажи"]


def test_extract_skips_header_word_row():
    grid = [["1", "Сотрудник", "Должность"], ["1", "Иванов", "Бухгалтер"]]
    mapping = {"data_start_row": 0, "columns": {"full_name": 1, "position": 2}, "sections": {}}
    recs = tablemap.extract_records(grid, mapping, required=["full_name"])
    assert recs == [{"full_name": "Иванов", "position": "Бухгалтер"}]


def test_map_columns_parses_sections():
    tablemap._llm = lambda s, u: '{"data_start_row":1,"columns":{"full_name":1,"position":2},"sections":{"department":0}}'
    fields = FIELDS + [{"name": "department", "description": "отдел"}]
    m = tablemap.map_columns(GRID, fields)
    assert m["columns"] == {"full_name": 1, "position": 2, "department": None}
    assert m["sections"] == {"department": 0}


def test_read_csv_grid_semicolon_cp1251():
    data = "№;ФИО;Должность\r\n1;Иванов;Бухгалтер\r\n".encode("cp1251")
    grid = tablemap.read_csv_grid(data)
    assert grid[0] == ["№", "ФИО", "Должность"]
    assert grid[1] == ["1", "Иванов", "Бухгалтер"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("ALL PASS")
