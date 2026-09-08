"""
Самопроверка расписания-инбокса и журнала действий против ОТДЕЛЬНОЙ тестовой БД.

Проверяет SQL-логику напрямую (без планировщика и файлов планов):
  - dispatch_due выпускает только наступившие сообщения (send_at <= now), будущие не трогает;
  - inbox/unread_count/mark_read работают по статусам;
  - activitylog.log пишет событие, recent его находит и фильтрует.

Нужен доступный Postgres. DSN:
    NEIROMASTER_TEST_DSN (по умолчанию postgresql://neiromaster:neiromaster@localhost:5432/neiromaster_test)
БД недоступна — печатает SKIP, не падает.

Запуск: python test_scheduling_logging.py
"""
import os
from datetime import datetime, timedelta, timezone

import db
import messaging
import activitylog

TEST_DSN = (os.environ.get("NEIROMASTER_TEST_DSN")
            or "postgresql://neiromaster:neiromaster@localhost:5432/neiromaster_test")


def _insert(row_id, emp, send_at, status="pending"):
    db.execute(
        "INSERT INTO scheduled_messages (id, employee_id, message_id, title, body, send_at, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (row_id, emp, row_id.split(":")[-1], "T", "B", send_at, status),
    )


def run():
    db.configure(TEST_DSN)
    db.init_schema()
    db.execute("DELETE FROM scheduled_messages")
    db.execute("DELETE FROM activity_log")

    now = datetime.now(timezone.utc)
    past = now - timedelta(hours=1)
    future = now + timedelta(days=1)

    # --- Доставка: наступившее -> delivered, будущее остаётся pending ---
    _insert("emp1:m_past", "emp1", past)
    _insert("emp1:m_future", "emp1", future)
    delivered = messaging.dispatch_due()
    assert delivered == 1, f"ожидалась 1 доставка, получено {delivered}"

    inbox = messaging.inbox("emp1")
    assert [m["message_id"] for m in inbox] == ["m_past"], inbox
    assert messaging.unread_count("emp1") == 1

    # Повторный прогон ничего не доставляет (идемпотентно)
    assert messaging.dispatch_due() == 0

    # --- Прочтение ---
    assert messaging.mark_read("emp1", "emp1:m_past") is True
    assert messaging.unread_count("emp1") == 0
    assert messaging.mark_read("emp1", "emp1:m_past") is False   # уже прочитано
    assert messaging.mark_read("emp1", "emp1:m_future") is False # ещё не доставлено

    # --- Журнал действий ---
    activitylog.log("login", user={"id": "u1", "username": "boss", "role": "owner"})
    activitylog.log("click", user={"id": "u1", "username": "boss", "role": "owner"},
                    detail={"el": "save"})
    logins = activitylog.recent(event_type="login")
    assert len(logins) == 1 and logins[0]["username"] == "boss", logins
    assert activitylog.recent(user_id="u1") and len(activitylog.recent(user_id="u1")) == 2

    print("test_scheduling_logging: OK")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        if any(s in str(e).lower() for s in ("connect", "timeout", "pool", "refused", "password")):
            print(f"SKIP: тестовая БД недоступна ({msg})")
        else:
            raise
