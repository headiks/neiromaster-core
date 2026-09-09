"""Логирование действий: приём клиентских событий (клики) и просмотр журнала админом."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

import activitylog
import users
from fastapi import HTTPException
from deps import current_user, require_admin, logged_in

router = APIRouter()


class EventRequest(BaseModel):
    type: str                      # только из activitylog.CLIENT_EVENTS
    path: str | None = None
    detail: dict | None = None


@router.post("/api/events", dependencies=logged_in)
async def ingest_event(req: EventRequest, request: Request, user: dict = Depends(current_user)):
    """Событие от фронта (клик, просмотр). Тип — только из белого списка; лишнее игнорируем."""
    event_type = req.type if req.type in activitylog.CLIENT_EVENTS else "click"
    activitylog.log(event_type, user=user, request=request,
                    path=req.path, detail=activitylog.clean_client_detail(req.detail))
    return {"ok": True}


@router.get("/api/activity")
async def list_activity(event_type: str | None = None, user_id: str | None = None,
                        limit: int = 200, actor: dict = Depends(require_admin)):
    """
    Журнал действий. Разграничение как на основной странице: суперадмин видит логи ВСЕХ,
    администратор — только пользователей СВОЕГО отдела (и свои). Фильтры: тип, пользователь.
    """
    if users.is_owner(actor):
        allowed = None                       # суперадмин — все
    else:
        allowed = [u["id"] for u in users.visible_users(actor, users.list_users())]

    if user_id:
        if allowed is not None and user_id not in allowed:
            raise HTTPException(status_code=403, detail="Пользователь не из вашего отдела")
        return {"events": activitylog.recent(limit=limit, event_type=event_type, user_id=user_id)}
    return {"events": activitylog.recent(limit=limit, event_type=event_type, user_ids=allowed)}
