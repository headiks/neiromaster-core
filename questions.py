"""
Очередь вопросов, на которые ассистент не смог ответить сам.

Сюда попадает вопрос сотрудника, когда:
  - маршрут «escalate» (ЧС/травма/конфликт — нужен человек), либо
  - маршрут «rag», но ответа нет: в базе не нашлось фрагментов или не пройден
    порог уверенности (confidence gate).

Детектирует эти случаи rag.handle_question (route + risk_flag), а маршрутизацию
человеку выполняет /ask в app.py: вопрос без ответа не теряется, а встаёт в очередь
администратору. Тот отвечает (обсудив со специалистом при необходимости), и ответ
сохраняется — сотрудник видит его в личном кабинете.

Хранилище: data/pending_questions.json (тот же файловый формат, что и users.json).
"""

import os
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from config import FileGuard

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
QUESTIONS_PATH = DATA_DIR / "pending_questions.json"

REASON_ESCALATE = "escalate"    # ЧС / риск — приоритетный разбор
REASON_NO_ANSWER = "no_answer"  # в регламентах ответа не нашлось

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

# Межпроцессная блокировка очереди вопросов: read-modify-write pending_questions.json
# безопасен и при нескольких uvicorn-воркерах (см. config.FileGuard). Прежний
# threading.Lock защищал только потоки одного процесса.
_lock = FileGuard(QUESTIONS_PATH.with_suffix(".lock"))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _load() -> dict:
    if not QUESTIONS_PATH.exists():
        return {}
    try:
        with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(items: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = QUESTIONS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    tmp.replace(QUESTIONS_PATH)


def record(user: dict, question: str, resolved_question: Optional[str],
           reason: str, risk_type: Optional[str] = None) -> dict:
    """Ставит вопрос сотрудника в очередь администратору. Возвращает созданную запись."""
    user = user or {}
    entry = {
        "id": str(uuid.uuid4()),
        "created_at": _now(),
        "user_id": user.get("id"),
        "user_name": user.get("full_name") or user.get("username") or "Сотрудник",
        "position": user.get("position") or "",
        "department": user.get("department") or "",
        "contact": user.get("contact") or "",
        "mentor": user.get("mentor") or "",
        "question": question,
        "resolved_question": resolved_question or None,
        "reason": reason if reason in (REASON_ESCALATE, REASON_NO_ANSWER) else REASON_NO_ANSWER,
        "risk_type": risk_type or None,
        "status": STATUS_OPEN,
        "answer": None,
        "answered_by": None,
        "answered_at": None,
    }
    with _lock:
        items = _load()
        items[entry["id"]] = entry
        _save(items)
    return dict(entry)


def _sort(entries: list) -> list:
    # Открытые вперёд, эскалации — в самый верх, дальше свежие раньше старых.
    return sorted(
        entries,
        key=lambda e: (
            e["status"] != STATUS_OPEN,
            e["reason"] != REASON_ESCALATE,
            e["created_at"],
        ),
        reverse=False,
    )


def list_all(status: Optional[str] = None) -> list:
    with _lock:
        items = list(_load().values())
    if status:
        items = [e for e in items if e["status"] == status]
    return _sort(items)


def list_for_user(user_id: str) -> list:
    with _lock:
        items = [e for e in _load().values() if e.get("user_id") == user_id]
    # Для сотрудника: сначала отвечённые (есть что прочитать), потом ожидающие; свежие выше.
    return sorted(items, key=lambda e: e["created_at"], reverse=True)


def resolve(qid: str, answer: str, admin_name: str) -> Optional[dict]:
    answer = (answer or "").strip()
    if not answer:
        raise ValueError("Ответ не может быть пустым")
    with _lock:
        items = _load()
        entry = items.get(qid)
        if not entry:
            return None
        entry["answer"] = answer
        entry["answered_by"] = admin_name or "Администратор"
        entry["answered_at"] = _now()
        entry["status"] = STATUS_RESOLVED
        _save(items)
    return dict(entry)


def count_open() -> int:
    with _lock:
        return sum(1 for e in _load().values() if e["status"] == STATUS_OPEN)


if __name__ == "__main__":
    # Мини-проверка: запись -> в очереди -> ответ -> ушла из открытых.
    QUESTIONS_PATH = DATA_DIR / "pending_questions.selftest.json"
    QUESTIONS_PATH.unlink(missing_ok=True)
    u = {"id": "u1", "full_name": "Иван Тест", "contact": "@ivan"}

    e1 = record(u, "Меня ударили", None, REASON_ESCALATE, "конфликт")
    e2 = record(u, "Сколько дней отпуска?", None, REASON_NO_ANSWER)
    assert count_open() == 2
    # Эскалация должна идти первой в списке открытых
    assert list_all(STATUS_OPEN)[0]["reason"] == REASON_ESCALATE
    assert len(list_for_user("u1")) == 2

    resolved = resolve(e2["id"], "  Отпуск 28 дней  ", "Админ")
    assert resolved["status"] == STATUS_RESOLVED
    assert resolved["answer"] == "Отпуск 28 дней"  # обрезка пробелов
    assert count_open() == 1

    try:
        resolve(e1["id"], "   ", "Админ")
        assert False, "пустой ответ должен падать"
    except ValueError:
        pass
    assert resolve("нет-такого", "x", "Админ") is None

    QUESTIONS_PATH.unlink(missing_ok=True)
    print("questions.py self-check OK")
