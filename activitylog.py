"""
Журнал действий пользователей: вход/выход, просмотры страниц, клики, ключевые действия.

Пишет в таблицу activity_log (schema-aware через db — попадает в схему текущего кабинета).
Главное правило: логирование НИКОГДА не роняет основную операцию — все ошибки глотаются.

Серверные события пишет сам бэкенд (login/logout в api_accounts, page_view в middleware
app.py, ключевые действия в роутерах). Клиентские клики принимает POST /api/events
(см. api_activity.py) — но только из белого списка типов, чтобы фронт не писал что попало.
"""
import json

from psycopg.types.json import Json

import db

# Что разрешено принимать от фронта через POST /api/events. Серверные типы
# (login, logout, login_failed, action) фронт слать не может — их ставит бэкенд.
CLIENT_EVENTS = {"click", "page_view"}
_MAX_DETAIL_BYTES = 4096   # ограничение размера detail от клиента


def log(event_type, user=None, request=None, path=None, detail=None):
    """Записать событие. user — запись пользователя (или None), request — FastAPI Request
    (из него берём ip/user-agent/path). Ошибки не пробрасываются."""
    try:
        ip = ua = None
        if request is not None:
            ip = request.client.host if request.client else None
            ua = request.headers.get("user-agent")
            if path is None:
                path = str(request.url.path)
        u = user or {}
        db.execute(
            "INSERT INTO activity_log "
            "(user_id, username, role, event_type, path, detail, ip, user_agent) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (u.get("id"), u.get("username"), u.get("role"),
             str(event_type)[:64], (path or None), Json(detail or {}), ip, ua),
        )
    except Exception:
        pass   # журнал не должен ломать основную операцию


def clean_client_detail(detail) -> dict:
    """Санитизация detail, пришедшего от фронта: только словарь и не больше лимита."""
    if not isinstance(detail, dict):
        return {}
    try:
        if len(json.dumps(detail, ensure_ascii=False).encode("utf-8")) > _MAX_DETAIL_BYTES:
            return {"_truncated": True}
    except (TypeError, ValueError):
        return {}
    return detail


def recent(limit=200, event_type=None, user_id=None):
    """Последние события для админского экрана. Фильтры опциональны."""
    where, params = [], []
    if event_type:
        where.append("event_type = %s"); params.append(event_type)
    if user_id:
        where.append("user_id = %s"); params.append(user_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit), 1000)))
    return db.query(
        f"SELECT id, ts, user_id, username, role, event_type, path, detail, ip, user_agent "
        f"FROM activity_log {clause} ORDER BY ts DESC LIMIT %s",
        tuple(params),
    )


if __name__ == "__main__":
    assert clean_client_detail({"el": "btn-save"}) == {"el": "btn-save"}
    assert clean_client_detail("nope") == {}
    assert clean_client_detail({"big": "x" * 5000}) == {"_truncated": True}
    assert "click" in CLIENT_EVENTS and "login" not in CLIENT_EVENTS
    print("activitylog: clean_client_detail — OK")
