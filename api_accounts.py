"""Вход, регистрация, свой профиль и свой личный кабинет."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

import auth
import users
import questions
import employees as adaptation
from deps import _set_session_cookie, current_user, require_setup_done, logged_in

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    full_name: str
    position: str | None = None
    contact: str | None = None


class CredentialsRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


# ---------- Вход, регистрация, свой профиль ----------
@router.post("/api/login")
async def api_login(req: LoginRequest, request: Request, response: Response):
    client = request.client.host if request.client else ""
    try:
        token, user = auth.login(req.username, req.password, client=client)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    _set_session_cookie(response, token)
    return {
        "username": user["username"],
        "role": user["role"],
        "must_change_credentials": bool(user.get("must_change_credentials")),
    }


@router.post("/api/register")
async def api_register(req: RegisterRequest):
    """Самостоятельная регистрация сотрудника."""
    try:
        user = users.register_employee(req.username, req.password, req.full_name,
                                       position=req.position or "", contact=req.contact or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": user["id"],
        "username": user["username"],
        "active": user["active"],
        "needs_approval": not user["active"],
    }


@router.post("/api/logout")
async def api_logout(request: Request, response: Response):
    auth.logout(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"logged_out": True}


@router.get("/api/me")
async def api_me(user: dict = Depends(current_user)):
    return users.public_view(user)


@router.post("/api/setup-credentials")
async def api_setup_credentials(req: CredentialsRequest, response: Response,
                                user: dict = Depends(current_user)):
    """
    Первичная настройка: пользователь заменяет выданные логин и пароль своими.
    Доступна только тем, у кого стоит флаг must_change_credentials.
    """
    if not user.get("must_change_credentials"):
        raise HTTPException(status_code=400, detail="Учётные данные уже настроены")
    try:
        users.set_credentials(user["id"], req.username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Логин сменился — старые сессии больше не действуют
    auth.drop_user_sessions(user["id"])
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"changed": True}


@router.post("/api/password")
async def api_change_password(req: PasswordChangeRequest, response: Response,
                              user: dict = Depends(current_user)):
    try:
        auth.change_own_password(user, req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Смена пароля разлогинивает все сессии, включая текущую
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"changed": True}


@router.get("/api/my/schedule", dependencies=logged_in)
async def api_my_schedule(user: dict = Depends(require_setup_done)):
    """Свой план адаптации — то, что сотрудник видит в личном кабинете."""
    try:
        return adaptation.build_employee_schedule(user)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/my/questions", dependencies=logged_in)
async def api_my_questions(user: dict = Depends(require_setup_done)):
    """Свои эскалированные вопросы и ответы на них от администратора."""
    return {"questions": questions.list_for_user(user["id"])}
