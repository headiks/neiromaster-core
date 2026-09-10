"""
Конвертеры унифицированного сообщения плана адаптации в конкретные форматы.

Генерация (planner.generate_substage_message) выдаёт ОДИН унифицированный формат
контента (envelope), независимо от типа подэтапа:

    {
      "format": "unified/1",
      "title": str,            # краткий заголовок
      "intro": str,            # дружелюбная краткая обвязка (приветствие)
      "body":  str,            # основной текст: факты из документов, без сокращений
      "key_points": [str],     # важные факты списком
      "questions": [           # для опроса/теста
         {"text": str, "type": "single|multi|bool|open",
          "options": [{"text": str, "correct": bool}], "explanation": str}
      ],
      "checklist": [str],      # для чек-листа
      "outro": str,            # краткое завершение / к кому обратиться
      "hr_note": str           # "Уточнить у HR: ..." или ""
    }

Отсюда бэкенд конвертирует его в нужный интерактивный формат: опрос (survey),
тест (quiz), чек-лист (checklist) или простое сообщение (message). Один источник —
много представлений, без повторной генерации.
"""

from typing import Optional

# Типы подэтапов -> целевой формат конвертации.
KIND_TO_FORMAT = {
    "survey": "survey",
    "quiz": "quiz",
    "checklist": "checklist",
    "message": "message",
    "reminder": "message",
    "system_check": "checklist",
    "handover": "message",
}


def _s(value) -> str:
    return str(value or "").strip()


def _list(value) -> list:
    return value if isinstance(value, list) else []


def to_text(content: dict) -> str:
    """Собирает читабельный текст сообщения из унифицированного envelope.
    Порядок: обвязка -> основной текст -> важные факты -> завершение -> заметка HR."""
    c = content or {}
    parts = []
    if _s(c.get("intro")):
        parts.append(_s(c["intro"]))
    if _s(c.get("body")):
        parts.append(_s(c["body"]))
    points = [_s(p) for p in _list(c.get("key_points")) if _s(p)]
    if points:
        parts.append("\n".join(f"• {p}" for p in points))
    checklist = [_s(p) for p in _list(c.get("checklist")) if _s(p)]
    if checklist:
        parts.append("\n".join(f"☐ {p}" for p in checklist))
    if _s(c.get("outro")):
        parts.append(_s(c["outro"]))
    if _s(c.get("hr_note")):
        parts.append(_s(c["hr_note"]))
    return "\n\n".join(parts).strip()


def _norm_options(raw) -> list:
    """Варианты ответа -> [{"id","text","correct"}]. Элемент — строка или {text,correct}."""
    out = []
    for i, opt in enumerate(_list(raw), 1):
        if isinstance(opt, dict):
            text, correct = _s(opt.get("text")), bool(opt.get("correct"))
        else:
            text, correct = _s(opt), False
        if text:
            out.append({"id": f"o{i}", "text": text, "correct": correct})
    return out


def _questions(content: dict) -> list:
    out = []
    for i, q in enumerate(_list((content or {}).get("questions")), 1):
        if not isinstance(q, dict):
            continue
        text = _s(q.get("text"))
        if not text:
            continue
        opts = _norm_options(q.get("options"))
        qtype = _s(q.get("type")) or ("open" if not opts else "single")
        out.append({
            "id": f"q{i}", "text": text, "type": qtype,
            "options": opts, "explanation": _s(q.get("explanation")),
        })
    return out


def to_message(content: dict) -> dict:
    return {"type": "message", "title": _s((content or {}).get("title")),
            "text": to_text(content)}


def to_survey(content: dict) -> dict:
    """Опрос: вопросы с вариантами, БЕЗ правильных ответов (сбор мнения)."""
    questions = [{
        "id": q["id"], "text": q["text"],
        "type": q["type"] if q["type"] in ("single", "multi", "open") else "single",
        "options": [{"id": o["id"], "text": o["text"]} for o in q["options"]],
    } for q in _questions(content)]
    return {"type": "survey", "title": _s((content or {}).get("title")),
            "intro": _s((content or {}).get("intro")) or to_text(content),
            "questions": questions}


def to_quiz(content: dict) -> dict:
    """Тест: вопросы с вариантами и отметкой правильного + пояснением."""
    questions = [{
        "id": q["id"], "text": q["text"],
        "type": q["type"] if q["type"] in ("single", "multi") else "single",
        "options": [{"id": o["id"], "text": o["text"], "correct": o["correct"]} for o in q["options"]],
        "explanation": q["explanation"],
    } for q in _questions(content)]
    return {"type": "quiz", "title": _s((content or {}).get("title")),
            "intro": _s((content or {}).get("intro")) or to_text(content),
            "questions": questions}


def to_checklist(content: dict) -> dict:
    """Чек-лист: пункты с отметкой выполнения. Источник — checklist или key_points."""
    c = content or {}
    items = [_s(p) for p in _list(c.get("checklist")) if _s(p)]
    if not items:
        items = [_s(p) for p in _list(c.get("key_points")) if _s(p)]
    return {"type": "checklist", "title": _s(c.get("title")),
            "intro": _s(c.get("intro")) or _s(c.get("body")),
            "items": [{"id": f"i{i}", "text": t, "done": False} for i, t in enumerate(items, 1)]}


_CONVERTERS = {"survey": to_survey, "quiz": to_quiz,
               "checklist": to_checklist, "message": to_message}


def convert(content: dict, kind: str) -> dict:
    """Конвертирует унифицированное сообщение в формат под тип подэтапа (kind)."""
    fmt = KIND_TO_FORMAT.get(kind, "message")
    return _CONVERTERS[fmt](content)


def _demo():
    c = {
        "format": "unified/1", "title": "Охрана труда",
        "intro": "Здравствуйте, [Имя]!", "body": "Носите каску в цехе.",
        "key_points": ["Каска обязательна", "Очки при сварке"],
        "questions": [{"text": "Что обязательно в цехе?", "type": "single",
                       "options": [{"text": "Каска", "correct": True},
                                   {"text": "Ничего", "correct": False}],
                       "explanation": "Каска защищает голову."}],
        "checklist": ["Получить каску", "Пройти инструктаж"],
        "outro": "Вопросы — наставнику.", "hr_note": "",
    }
    assert "Каска обязательна" in to_text(c)
    survey = convert(c, "survey")
    assert survey["type"] == "survey"
    assert "correct" not in survey["questions"][0]["options"][0]   # опрос без правильных
    quiz = convert(c, "quiz")
    assert quiz["questions"][0]["options"][0]["correct"] is True    # тест с правильными
    chk = convert(c, "checklist")
    assert chk["items"][0]["text"] == "Получить каску" and chk["items"][0]["done"] is False
    assert convert(c, "reminder")["type"] == "message"             # напоминание -> сообщение
    # пустой контент не падает
    assert convert({}, "quiz")["questions"] == []
    print("msgconvert demo ok")


if __name__ == "__main__":
    _demo()
