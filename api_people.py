"""Люди: импорт штатного расписания, профили, роли, назначение планов адаптации."""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Response
import json
from urllib.parse import quote

from pydantic import BaseModel

import auth
import users
import staffing
import planner
import employees as adaptation
from config import MAX_UPLOAD_BYTES
from deps import require_admin, require_owner, admin_only, owner_only

router = APIRouter()


# ---------- Штатное расписание (первичный инструмент: люди и должности) ----------
class StaffingImportRequest(BaseModel):
    records: list = []


@router.post("/staffing/preview", dependencies=admin_only)
async def staffing_preview(file: UploadFile = File(...)):
    """Разбор загруженной xlsx-штатки: ИИ определяет разметку столбцов, возвращаем
    найденное сопоставление и извлечённые записи для подтверждения администратором."""
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    try:
        result = staffing.parse_file(content, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось разобрать таблицу: {e}")
    result["count"] = len(result.get("records") or [])
    return result


@router.post("/staffing/import", dependencies=admin_only)
async def staffing_import(req: StaffingImportRequest):
    """Массовое создание из подтверждённых строк единой таблицы: строки с ФИО — профили
    сотрудников (логины/пароли в ответе, один раз), строки без ФИО — профили-вакансии."""
    return staffing.import_records(req.records or [])


@router.post("/users/delete-non-admins", dependencies=owner_only)
async def delete_non_admins():
    """Удаляет ВСЕХ пользователей, кроме администраторов (владелец и админы остаются).
    Необратимо — только для главного администратора. Заодно чистит строки рассылки."""
    deleted = 0
    for u in users.list_users():
        if u.get("role") in users.ADMIN_ROLES:
            continue
        if users.delete_user(u["id"]):
            deleted += 1
    return {"deleted": deleted}


# ---------- Пользователи: профили, роли, назначение планов ----------
class UserRequest(BaseModel):
    full_name: str
    username: str | None = None
    password: str | None = None
    position: str | None = None
    department: str | None = None
    contact: str | None = None
    mentor: str | None = None
    manager: str | None = None
    plan_id: str | None = None
    start_date: str | None = None
    status: str | None = None
    notes: str | None = None


class RoleRequest(BaseModel):
    role: str


class ActiveRequest(BaseModel):
    active: bool


class TargetCredentialsRequest(BaseModel):
    username: str | None = None
    password: str | None = None


def _target_user(user_id: str, actor: dict) -> dict:
    """Находит пользователя и проверяет, что актор вправе его менять."""
    target = users.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        users.ensure_can_manage(actor, target)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return target


@router.get("/users")
async def get_users(actor: dict = Depends(require_admin)):
    """Суперадмин видит всех, администратор — только людей своего отдела (и себя)."""
    plans = {p["plan_id"]: p for p in planner.list_plans()}
    result = []
    for user in users.visible_users(actor, users.list_users()):
        plan = plans.get(user.get("plan_id"))
        result.append({
            **user,
            "plan_title": plan["title"] if plan else None,
            "plan_generated": bool(plan and plan.get("generated")),
            "has_account": bool(user.get("username")),
        })
    return {"users": result, "scope": "all" if users.is_owner(actor) else "department"}


@router.post("/users", dependencies=admin_only)
async def create_user(req: UserRequest, actor: dict = Depends(require_admin)):
    """
    Заведение сотрудника администратором. Логин и временный пароль необязательны:
    профиль можно создать заранее, а доступ выдать позже.
    """
    payload = req.model_dump()
    # Отдел по умолчанию — отдел заводящего администратора: иначе он создаст человека,
    # которого сам же не увидит (список людей отфильтрован по отделу).
    if not users.is_owner(actor) and not (payload.get("department") or "").strip():
        payload["department"] = actor.get("department") or ""
    try:
        user = users.create_user(payload, actor=actor, role=users.ROLE_EMPLOYEE,
                                 must_change_credentials=bool(req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return users.public_view(user)


@router.get("/users/{user_id}")
async def get_user(user_id: str, actor: dict = Depends(require_admin)):
    return users.public_view(_target_user(user_id, actor))


@router.put("/users/{user_id}")
async def update_user(user_id: str, req: UserRequest, actor: dict = Depends(require_admin)):
    """Правка профиля и назначение плана адаптации с датой выхода."""
    _target_user(user_id, actor)
    user = users.update_profile(user_id, req.model_dump())
    return users.public_view(user)


@router.delete("/users/{user_id}", dependencies=owner_only)
async def remove_user(user_id: str, actor: dict = Depends(require_owner)):
    """Удаление пользователя — только главный администратор."""
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    try:
        deleted = users.delete_user(user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    auth.drop_user_sessions(user_id)
    return {"user_id": user_id, "deleted": True}


@router.post("/users/{user_id}/role", dependencies=owner_only)
async def change_user_role(user_id: str, req: RoleRequest, actor: dict = Depends(require_owner)):
    """Назначить администратором или убрать из администраторов — только главный."""
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Нельзя изменить собственную роль")
    try:
        user = users.set_role(user_id, req.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Права изменились — пусть перезайдёт с актуальной ролью
    auth.drop_user_sessions(user_id)
    return users.public_view(user)


@router.post("/users/{user_id}/active")
async def change_user_active(user_id: str, req: ActiveRequest, actor: dict = Depends(require_admin)):
    """
    Подтверждение регистрации и блокировка доступа. Администратор может
    активировать и блокировать сотрудников, главный администратор — кого угодно.
    """
    _target_user(user_id, actor)
    if user_id == actor["id"]:
        raise HTTPException(status_code=400, detail="Нельзя заблокировать самого себя")
    try:
        user = users.set_active(user_id, req.active)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not req.active:
        auth.drop_user_sessions(user_id)
    return users.public_view(user)


@router.post("/users/{user_id}/credentials")
async def set_user_credentials(user_id: str, req: TargetCredentialsRequest,
                               actor: dict = Depends(require_admin)):
    """
    Выдача логина и/или временного пароля. Пользователь при следующем входе
    обязан задать свои учётные данные.
    """
    _target_user(user_id, actor)
    try:
        if req.username:
            users.set_username(user_id, req.username)
        if req.password:
            users.set_password(user_id, req.password, must_change=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.password:
        auth.drop_user_sessions(user_id)
    return users.public_view(users.get_user(user_id))


@router.post("/users/{user_id}/transfer-ownership", dependencies=owner_only)
async def transfer_ownership(user_id: str, actor: dict = Depends(require_owner)):
    """Передача роли главного администратора. Прежний владелец остаётся админом."""
    try:
        new_owner = users.transfer_ownership(actor["id"], user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Роли поменялись у обоих — обе сессии переоформляются входом заново
    auth.drop_user_sessions(user_id)
    auth.drop_user_sessions(actor["id"])
    return users.public_view(new_owner)


@router.get("/users/{user_id}/schedule")
async def get_user_schedule(user_id: str, actor: dict = Depends(require_admin)):
    """
    Персональное расписание: план-шаблон, пересчитанный на дату выхода этого
    сотрудника, с подстановкой плейсхолдеров. Считается на лету — при правке
    плана или даты выхода расписание всегда актуальное.
    """
    user = _target_user(user_id, actor)
    try:
        return adaptation.build_employee_schedule(user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


EMPLOYEE_EXPORTS = {"schedule.json", "schedule.md"}


@router.get("/users/{user_id}/export/{name}")
async def export_user_schedule(user_id: str, name: str, actor: dict = Depends(require_admin)):
    if name not in EMPLOYEE_EXPORTS:
        raise HTTPException(status_code=400, detail=f"Доступны: {', '.join(EMPLOYEE_EXPORTS)}")

    employee = _target_user(user_id, actor)
    try:
        schedule = adaptation.build_employee_schedule(employee)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if name == "schedule.json":
        body = json.dumps(schedule, ensure_ascii=False, indent=2)
        media_type = "application/json"
    else:
        body = planner.render_schedule_md(schedule)
        media_type = "text/markdown; charset=utf-8"

    # ФИО кириллицей в заголовок напрямую не положить (HTTP-заголовки — latin-1),
    # поэтому ASCII-запаска плюс RFC 5987 filename* с процентным кодированием
    pretty = f"{(employee.get('full_name') or 'employee').replace(' ', '_')}_{name}"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition":
                 f'attachment; filename="{user_id}_{name}"; '
                 f"filename*=UTF-8''{quote(pretty)}"},
    )
