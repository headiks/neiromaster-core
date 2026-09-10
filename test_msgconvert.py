"""Конвертер унифицированного сообщения -> опрос/тест/чек-лист/сообщение.
Чистая логика, без сети/БД. Запуск: python test_msgconvert.py"""
import msgconvert


def test_convert():
    msgconvert._demo()


if __name__ == "__main__":
    test_convert()
    print("test_msgconvert: OK")
