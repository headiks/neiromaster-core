"""
Доставка сообщений плана адаптации по расписанию.

Расписание считается на лету (employees.build_employee_schedule), но чтобы доставлять
сообщения в нужный момент и не слать дважды, каждое сообщение материализуется строкой в
таблице scheduled_messages (см. db.py). Эта же таблица — инбокс сотрудника.

Жизненный цикл сообщения:
    pending  — материализовано, время ещё не наступило
    delivered — планировщик выпустил его в кабинет (send_at <= now)
    read     — сотрудник открыл
Доставка = смена pending -> delivered (без внешних каналов; сотрудник видит его в кабинете).

Планировщик: фоновый цикл в приложении (start_scheduler) ИЛИ разовый прогон из CLI
(dispatch_messages.py). Оба зовут dispatch_all(): по каждой схеме (public + все кабинеты
из public.cabinets) — досоздать недостающие строки и выпустить наступившие.

ponytail: время сообщения берётся в таймзоне плана (одна на план); индивидуальная
таймзона сотрудника не моделируется — добавить поле, если понадобится.
"""
import os
import time
import threading
import contextlib
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import users
import employees as adaptation
from planner import DEFAULT_TIMEZONE

_started = False


def _localize(iso_naive: str, tzname: str) -> datetime:
    """Наивное локальное время плана ('2026-09-10T09:00') -> абсолютный момент с таймзоной."""
    dt = datetime.fromisoformat(iso_naive)
    try:
        tz = ZoneInfo(tzname or DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    return dt.replace(tzinfo=tz)


def _message_row(employee_id: str, plan_id, msg: dict, tzname: str):
    """Готовит поля строки scheduled_messages из позиции расписания. Чистая (тестируется)."""
    send_iso = (msg.get("schedule") or {}).get("send_at")
    if not send_iso:
        return None
    stage = msg.get("stage") or {}
    sub = msg.get("substage") or {}
    title = " — ".join(p for p in (stage.get("title"), sub.get("title")) if p)
    body = (msg.get("content") or {}).get("text", "") or ""
    return {
        "id": f"{employee_id}:{msg['message_id']}",
        "employee_id": employee_id,
        "plan_id": plan_id,
        "message_id": msg["message_id"],
        "stage_id": stage.get("id"),
        "substage_id": sub.get("id"),
        "title": title,
        "body": body,
        "send_at": _localize(send_iso, tzname),
    }


# ---------- Материализация ----------
def materialize_employee(employee: dict, force: bool = False) -> int:
    """Создаёт/обновляет строки расписания для сотрудника. Возвращает число строк.
    force=True пересобирает будущие (pending) строки, доставленные/прочитанные не трогает."""
    try:
        schedule = adaptation.build_employee_schedule(employee)
    except ValueError:
        return 0   # плана или даты выхода нет — материализовать нечего

    tzname = schedule.get("timezone") or DEFAULT_TIMEZONE
    plan_id = schedule.get("plan_id")
    rows = [r for r in (_message_row(employee["id"], plan_id, m, tzname)
                        for m in schedule.get("messages", [])) if r]
    if not rows:
        return 0

    if force:
        db.execute("DELETE FROM scheduled_messages WHERE employee_id = %s AND status = 'pending'",
                   (employee["id"],))
    for r in rows:
        # Обновляем только ещё не доставленные строки — историю не переписываем.
        db.execute(
            "INSERT INTO scheduled_messages "
            "(id, employee_id, plan_id, message_id, stage_id, substage_id, title, body, send_at) "
            "VALUES (%(id)s, %(employee_id)s, %(plan_id)s, %(message_id)s, %(stage_id)s, "
            "%(substage_id)s, %(title)s, %(body)s, %(send_at)s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  title = EXCLUDED.title, body = EXCLUDED.body, send_at = EXCLUDED.send_at, "
            "  plan_id = EXCLUDED.plan_id, stage_id = EXCLUDED.stage_id, "
            "  substage_id = EXCLUDED.substage_id, updated_at = now() "
            "WHERE scheduled_messages.status = 'pending'",
            r,
        )
    return len(rows)


def ensure_all() -> int:
    """Досоздаёт строки для сотрудников с планом и датой выхода, у которых их ещё нет.
    Дёшево в устойчивом состоянии: после первого прохода у всех строки уже есть."""
    present = {r["employee_id"] for r in
              db.query("SELECT DISTINCT employee_id FROM scheduled_messages")}
    made = 0
    for u in users.list_users():
        if u.get("role") != users.ROLE_EMPLOYEE:
            continue
        if not u.get("plan_id") or not u.get("start_date") or u["id"] in present:
            continue
        made += materialize_employee(u)
    return made


# ---------- Доставка ----------
def dispatch_due() -> int:
    """Выпускает в кабинет все наступившие сообщения текущей схемы. Возвращает число."""
    rows = db.query(
        "UPDATE scheduled_messages SET status = 'delivered', delivered_at = now(), "
        "updated_at = now() WHERE status = 'pending' AND send_at <= now() RETURNING id",
        fetch="all",
    )
    return len(rows or [])


def _schemas():
    """Схемы для обработки: public (None) + все кабинеты из реестра."""
    yield None
    try:
        for r in db.query("SELECT schema_name FROM public.cabinets"):
            yield r["schema_name"]
    except Exception:
        pass   # реестра кабинетов ещё нет — работаем только с public


def dispatch_all() -> int:
    """Один проход по всем схемам: досоздать недостающее и выпустить наступившее."""
    total = 0
    for schema in _schemas():
        ctx = db.use_schema(schema) if schema else contextlib.nullcontext()
        with ctx:
            try:
                ensure_all()
                total += dispatch_due()
            except Exception as e:
                print(f"[scheduler] схема {schema or 'public'}: {e}")
    return total


# ---------- Инбокс сотрудника ----------
def inbox(employee_id: str, limit: int = 200) -> list:
    return db.query(
        "SELECT id, message_id, title, body, send_at, status, delivered_at, read_at, "
        "stage_id, substage_id FROM scheduled_messages "
        "WHERE employee_id = %s AND status IN ('delivered', 'read') "
        "ORDER BY send_at DESC LIMIT %s",
        (employee_id, max(1, min(int(limit), 500))),
    )


def unread_count(employee_id: str) -> int:
    r = db.query("SELECT count(*) AS n FROM scheduled_messages "
                 "WHERE employee_id = %s AND status = 'delivered'", (employee_id,), "one")
    return r["n"] if r else 0


def mark_read(employee_id: str, message_row_id: str) -> bool:
    rows = db.query(
        "UPDATE scheduled_messages SET status = 'read', read_at = now(), updated_at = now() "
        "WHERE id = %s AND employee_id = %s AND status = 'delivered' RETURNING id",
        (message_row_id, employee_id), fetch="all",
    )
    return bool(rows)


# ---------- Планировщик (фоновый цикл) ----------
def _loop(interval: int):
    while True:
        try:
            dispatch_all()
        except Exception as e:
            print(f"[scheduler] сбой прохода: {e}")
        time.sleep(interval)


def start_scheduler():
    """Запуск фонового цикла доставки. Отключается NEIROMASTER_SCHEDULER=0.
    Интервал опроса — NEIROMASTER_SCHEDULER_INTERVAL секунд (по умолчанию 60)."""
    global _started
    if _started or os.environ.get("NEIROMASTER_SCHEDULER", "1").lower() in ("0", "false", "no"):
        return
    _started = True
    interval = max(5, int(os.environ.get("NEIROMASTER_SCHEDULER_INTERVAL", "60")))
    threading.Thread(target=_loop, args=(interval,), daemon=True).start()
    print(f"[scheduler] фоновый цикл доставки запущен (каждые {interval} с)")


if __name__ == "__main__":
    msg = {
        "message_id": "s1.a", "schedule": {"send_at": "2026-09-10T09:00"},
        "stage": {"id": "s1", "title": "Вводный"},
        "substage": {"id": "a", "title": "Знакомство"},
        "content": {"text": "Привет!"},
    }
    row = _message_row("emp1", "plan1", msg, "Europe/Moscow")
    assert row["id"] == "emp1:s1.a", row
    assert row["title"] == "Вводный — Знакомство"
    assert row["body"] == "Привет!"
    assert row["send_at"].utcoffset() is not None            # локализовано (aware)
    assert row["send_at"].hour == 9 and row["stage_id"] == "s1"
    assert _message_row("e", "p", {"message_id": "x", "schedule": {}}, "UTC") is None  # без send_at
    print("messaging: _message_row/_localize — OK")
