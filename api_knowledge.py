"""Этапы обучения и очередь вопросов без ответа (эскалация человеку)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import stages
import questions
from deps import require_admin, admin_only

router = APIRouter()


# ---------- Этапы обучения (структура, к которой привязываются папки) ----------
class StageRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    substages: list | None = None
    position: int | None = None


@router.get("/knowledge/stages", dependencies=admin_only)
async def get_stages():
    return {"stages": stages.list_stages()}


@router.post("/knowledge/stages", dependencies=admin_only)
async def create_stage(req: StageRequest):
    try:
        return stages.create_stage(req.title, req.description or "", req.substages)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/knowledge/stages/{stage_id}", dependencies=admin_only)
async def update_stage(stage_id: str, req: StageRequest):
    if stages.get_stage(stage_id) is None:
        raise HTTPException(status_code=404, detail="Этап не найден")
    try:
        return stages.update_stage(stage_id, **req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/knowledge/stages/{stage_id}", dependencies=admin_only)
async def delete_stage(stage_id: str):
    if not stages.delete_stage(stage_id):
        raise HTTPException(status_code=404, detail="Этап не найден")
    return {"deleted": True}


# ---------- Вопросы без ответа (эскалация человеку) ----------
class ResolveRequest(BaseModel):
    answer: str


@router.get("/questions", dependencies=admin_only)
async def get_questions(status: str | None = "open"):
    """Очередь вопросов сотрудников, на которые ассистент не ответил сам."""
    return {"questions": questions.list_all(status=status or None),
            "open_count": questions.count_open()}


@router.post("/questions/{qid}/resolve", dependencies=admin_only)
async def resolve_question(qid: str, req: ResolveRequest, actor: dict = Depends(require_admin)):
    """Администратор отвечает на вопрос — ответ уходит в личный кабинет сотрудника."""
    try:
        entry = questions.resolve(qid, req.answer, actor.get("full_name") or actor.get("username"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if entry is None:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    return entry
