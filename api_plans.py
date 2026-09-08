"""Конструктор плана адаптации: планы, генерация сообщений, экспорт, фоновые задачи."""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from fastapi.responses import FileResponse

import db
import users
import planner
import indexing
import messaging
import activitylog
from deps import _bg, admin_only, require_admin

router = APIRouter()


# ---------- Конструктор плана адаптации ----------
class PlanRequest(BaseModel):
    title: str | None = None
    role: str | None = None
    description: str | None = None
    start_date: str | None = None
    timezone: str | None = None
    stages: list = []


@router.get("/catalog", dependencies=admin_only)
async def get_catalog():
    """Каталог этапов и шаблонов подэтапов — из него человек собирает план."""
    return planner.load_catalog()


@router.get("/plans", dependencies=admin_only)
async def get_plans():
    return {"plans": planner.list_plans()}


@router.post("/plans")
async def create_plan(req: PlanRequest, user: dict = Depends(require_admin)):
    plan = planner.normalize_plan(req.model_dump())
    planner.save_plan(plan)
    activitylog.log("action", user=user, path="/plans",
                    detail={"action": "plan_create", "plan_id": plan.get("id"),
                            "title": plan.get("title")})
    return plan


@router.post("/plans/template", dependencies=admin_only)
async def create_full_template(title: str | None = None):
    """Создаёт полный универсальный шаблон плана из всего каталога (все этапы и подэтапы),
    единый для всех профессий. Дальше редактируется как обычный план."""
    plan = planner.build_full_template(title or "Универсальный план адаптации")
    planner.save_plan(plan)
    return plan


@router.post("/chunks/assign-stages", dependencies=admin_only)
def assign_chunks_to_stages():
    """Материализация «папок этапов»: раскладывает все чанки по этапам каталога адаптации
    (payload.plan_stages). Нужно после загрузки документов, чтобы генерация плана брала
    чанки нужного этапа. Фоново — прогресс тянуть общей ручкой задач не нужно, операция
    дешёвая (эмбеддинги этапов + косинус к готовым векторам)."""
    _bg(indexing.assign_chunks_to_stages)
    return {"status": "started"}


@router.get("/plans/{plan_id}", dependencies=admin_only)
async def get_plan(plan_id: str):
    plan = planner.load_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    return {"plan": plan, "schedule_preview": planner.resolve_schedule(plan)}


@router.put("/plans/{plan_id}", dependencies=admin_only)
async def update_plan(plan_id: str, req: PlanRequest):
    existing = planner.load_plan(plan_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="План не найден")
    payload = req.model_dump()
    payload["created_at"] = existing.get("created_at")
    plan = planner.normalize_plan(payload, plan_id=plan_id)
    planner.save_plan(plan)
    return plan


@router.delete("/plans/{plan_id}", dependencies=admin_only)
async def remove_plan(plan_id: str):
    if not planner.delete_plan(plan_id):
        raise HTTPException(status_code=404, detail="План не найден")
    return {"plan_id": plan_id, "deleted": True}


@router.post("/plans/{plan_id}/duplicate", dependencies=admin_only)
async def duplicate_plan(plan_id: str, title: str | None = None):
    """Копия плана под смежную должность — дальше редактируется как обычно."""
    plan = planner.duplicate_plan(plan_id, title)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    return plan


def _staffing_positions() -> list:
    """Уникальные должности сотрудников из штатки — под каждую генерируется свой контент плана."""
    seen = []
    for u in users.list_users():
        pos = (u.get("position") or "").strip()
        if pos and pos not in seen:
            seen.append(pos)
    return seen


@router.post("/plans/{plan_id}/generate", dependencies=admin_only)
async def generate_plan(plan_id: str):
    """
    Запускает фоновую генерацию контента плана. План один; контент собирается под КАЖДУЮ
    уникальную должность из штатки (плюс общее расписание). Прогресс — через GET /jobs/{job_id}.
    """
    plan = planner.load_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    if not any(s.get("substages") for s in plan.get("stages") or []):
        raise HTTPException(status_code=400, detail="В плане нет ни одного подэтапа")
    return planner.start_generation(plan, positions=_staffing_positions())


@router.post("/plans/{plan_id}/rollout")
async def rollout_plan(plan_id: str, user: dict = Depends(require_admin)):
    """Применить готовый план ко всем сотрудникам: назначить план каждому сотруднику и
    запустить фоновую генерацию содержания под каждую уникальную должность из штатки.
    Прогресс — через GET /jobs/{job_id}. Сотрудник дальше видит план своей профессии."""
    plan = planner.load_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    if not any(s.get("substages") for s in plan.get("stages") or []):
        raise HTTPException(status_code=400, detail="В плане нет ни одного подэтапа")
    # Назначаем план всем сотрудникам, чтобы каждый увидел его в кабинете (расписание
    # подставляется под его должность). Роли админа/владельца не трогаем.
    db.execute("UPDATE users SET plan_id = %s WHERE role = %s", (plan_id, users.ROLE_EMPLOYEE))
    activitylog.log("action", user=user, path=f"/plans/{plan_id}/rollout",
                    detail={"action": "plan_rollout", "plan_id": plan_id})
    # Материализуем расписание-инстансы сразу (у кого есть дата выхода), не дожидаясь
    # следующего тика планировщика — фоново, чтобы не держать ответ.
    _bg(messaging.ensure_all)
    return planner.start_generation(plan, positions=_staffing_positions())


@router.get("/jobs/{job_id}", dependencies=admin_only)
async def get_job(job_id: str):
    job = planner.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job


@router.post("/jobs/{job_id}/cancel", dependencies=admin_only)
async def cancel_generation(job_id: str):
    """Отмена фоновой генерации: уже сгенерированные подэтапы сохраняются, дальше не идём."""
    job = planner.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job


@router.get("/plans/{plan_id}/schedule", dependencies=admin_only)
async def get_schedule(plan_id: str, profession: str | None = None):
    """Расписание плана. profession — показать вариант под конкретную должность (иначе общий)."""
    schedule = planner.load_schedule(plan_id, profession or "")
    if schedule is None:
        raise HTTPException(status_code=404, detail="Расписание ещё не сгенерировано")
    return schedule


@router.get("/plans/{plan_id}/professions", dependencies=admin_only)
async def get_plan_professions(plan_id: str):
    """Профессии, под которые уже сгенерированы отдельные расписания."""
    return {"professions": planner.list_schedule_professions(plan_id)}


@router.post("/plans/{plan_id}/messages/{message_id}/regenerate", dependencies=admin_only)
def regenerate_message(plan_id: str, message_id: str, profession: str | None = None):
    """Перегенерация одного подэтапа — без прогона всего плана. profession — какое расписание."""
    plan = planner.load_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    message = planner.regenerate_one(plan, message_id, profession or "")
    if message is None:
        raise HTTPException(status_code=404, detail="Подэтап не найден в плане")
    return message


EXPORT_FILES = {
    "plan.md": ("text/markdown; charset=utf-8", "План в формате для LLM"),
    "plan.json": ("application/json", "Канонический план"),
    "schedule.md": ("text/markdown; charset=utf-8", "Расписание с готовыми ответами"),
    "schedule.json": ("application/json", "Расписание для мессенджеров и приложения"),
}


@router.get("/plans/{plan_id}/export/{name}", dependencies=admin_only)
async def export_plan(plan_id: str, name: str):
    if name not in EXPORT_FILES:
        raise HTTPException(status_code=400, detail=f"Доступны: {', '.join(EXPORT_FILES)}")
    try:
        path = planner.plan_dir(plan_id) / name
    except ValueError:
        raise HTTPException(status_code=404, detail="План не найден")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл ещё не сформирован")
    media_type, _ = EXPORT_FILES[name]
    return FileResponse(path, media_type=media_type, filename=f"{plan_id}_{name}")
