"""Логирование действий: приём клиентских событий (клики) и просмотр журнала админом."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

import activitylog
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


@router.get("/api/activity", dependencies=[Depends(require_admin)])
async def list_activity(event_type: str | None = None, user_id: str | None = None,
                        limit: int = 200):
    """Журнал действий для админского экрана. Фильтры: тип события, пользователь."""
    return {"events": activitylog.recent(limit=limit, event_type=event_type, user_id=user_id)}
