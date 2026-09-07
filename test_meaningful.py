"""Проверка фильтра смысловой нагрузки чанка (classify.is_meaningful) — чистая логика,
без сети/Qdrant. Мусор (заголовки, номера страниц, оглавление) не должен распределяться."""

import test_stubs

# classify НЕ подменяем — его и тестируем
test_stubs.install(embed_dim=8, stub_modules=("folders",))

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
