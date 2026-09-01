"""
Персональное расписание адаптации сотрудника.

Администратор назначает сотруднику готовый план-шаблон и дату выхода на работу
(поля plan_id и start_date в записи пользователя, см. users.py). Расписание
считается на лету:

    план (относительные смещения)
        + дата выхода сотрудника        -> конкретные дата и время каждого сообщения
        + сгенерированные тексты плана  -> содержимое сообщений
        + данные сотрудника             -> подстановка плейсхолдеров [Имя] и т.п.

Тексты под каждого сотрудника заново не генерируются: они зависят от документов
компании, а не от человека. Один сгенерированный план обслуживает сколько угодно
новичков — меняются только даты и плейсхолдеры.
"""

import time
from typing import Optional

import planner


def first_name(full_name: str) -> str:
    """«Иванов Иван Иванович» -> «Иван». Если ФИО из одного слова — берём его."""
    parts = [p for p in (full_name or "").split() if p]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else "коллега"


def substitute(text: str, employee: dict) -> str:
    """Подстановка плейсхолдеров, которые генератор оставляет в тексте сообщения."""
    if not text:
        return text
    replacements = {
        "[Имя]": first_name(employee.get("full_name", "")),
        "[ФИО]": employee.get("full_name", ""),
        "[Должность]": employee.get("position", ""),
    }
    if employee.get("mentor"):
        replacements["[ФИО наставника]"] = employee["mentor"]
        replacements["[ФИО, тел.]"] = employee["mentor"]
    if employee.get("manager"):
        replacements["[ФИО руководителя]"] = employee["manager"]

    for placeholder, value in replacements.items():
        if value:
            text = text.replace(placeholder, value)
    return text


def build_employee_schedule(employee: dict) -> dict:
    """
    Собирает персональное расписание сотрудника.
    Поднимает ValueError, если план не назначен, не найден или нет даты выхода.
    """
    plan_id = employee.get("plan_id")
    if not plan_id:
        raise ValueError("Сотруднику не назначен план адаптации")
    if not employee.get("start_date"):
        raise ValueError("У сотрудника не указана дата выхода на работу")

    plan = planner.load_plan(plan_id)
    if plan is None:
        raise ValueError("Назначенный план не найден — возможно, он удалён")

    # Считаем смещения по дате выхода именно этого сотрудника
    items = planner.resolve_schedule({**plan, "start_date": employee["start_date"]})

    # Контент — под должность сотрудника (расписание её профессии); если своего нет — общее.
    schedule = planner.load_schedule(plan_id, employee.get("position") or "")
    generated = {m["message_id"]: m for m in (schedule or {}).get("messages", [])}

    messages = []
    for item in items:
        source = generated.get(item["message_id"], {})
        content = dict(source.get("content") or {"format": "markdown", "text": ""})
        content["text"] = substitute(content.get("text", ""), employee)
        messages.append({
            **item,
            "content": content,
            # В расписании планировщика поле называется topics_used (см. planner.build_schedule);
            # отдаём его под ключом folders_used, который читает админский обзор расписания.
            "folders_used": source.get("topics_used", []),
            "sources": source.get("sources", []),
            "actions": source.get("actions", []),
            "status": source.get("status", "pending"),
            "error": source.get("error"),
        })

    return {
        "schema_version": planner.SCHEMA_VERSION,
        "employee": {
            "id": employee["id"],
            "full_name": employee.get("full_name"),
            "position": employee.get("position"),
            "department": employee.get("department"),
            "contact": employee.get("contact"),
            "mentor": employee.get("mentor"),
            "manager": employee.get("manager"),
            "status": employee.get("status"),
        },
        "plan_id": plan_id,
        "plan_title": plan.get("title"),
        "role": plan.get("role"),
        "start_date": employee["start_date"],
        "timezone": plan.get("timezone", planner.DEFAULT_TIMEZONE),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "plan_generated": schedule is not None,
        "messages": messages,
    }


def upcoming_messages(schedule: dict, limit: int = 5, today: Optional[str] = None) -> list:
    """Ближайшие сообщения от сегодняшнего дня — для личного кабинета сотрудника."""
    today = today or time.strftime("%Y-%m-%d")
    future = [m for m in schedule.get("messages") or []
              if (m["schedule"].get("send_at") or "") >= today]
    return future[:limit]
