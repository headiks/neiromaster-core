"""
HTML-страницы приложения. Логики нет — только проверка доступа и отдача файла.

У всех админских страниц проверка одна и та же (вошёл -> прошёл первичную
настройку -> администратор), поэтому она вынесена в deps.page_for_admin, а не
скопирована в каждый обработчик.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import auth
import users
from deps import _read_static, page_for_admin

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Личный кабинет сотрудника: чат с ассистентом и свой план адаптации."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_credentials"):
        return RedirectResponse(url="/setup", status_code=303)
    return HTMLResponse(_read_static("index.html"))


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return _read_static("login.html")


@router.get("/register", response_class=HTMLResponse)
async def register_page():
    return _read_static("register.html")


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Первичная настройка: замена выданных логина и пароля своими."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not user.get("must_change_credentials"):
        return RedirectResponse(url="/admin" if users.is_admin(user) else "/", status_code=303)
    return HTMLResponse(_read_static("setup.html"))


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Админка: база знаний, конструктор плана, пользователи."""
    return page_for_admin(request, "admin.html")


@router.get("/s3", response_class=HTMLResponse)
async def s3_page(request: Request):
    """Обозреватель S3-хранилища оригиналов (только чтение, для админа)."""
    return page_for_admin(request, "s3_browser.html")


@router.get("/documents-board", response_class=HTMLResponse)
async def documents_board_page(request: Request):
    """Экран «этапы ↔ документы»: какие документы закреплены за этапами и подэтапами."""
    return page_for_admin(request, "documents_board.html")


@router.get("/documents-table", response_class=HTMLResponse)
async def documents_table_page(request: Request):
    """Табличный просмотр метаданных обработанных файлов (реестр documents)."""
    return page_for_admin(request, "documents_table.html")


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Журнал действий по пользователям (суперадмин — все, администратор — свой отдел)."""
    return page_for_admin(request, "logs.html")


@router.get("/plans-db", response_class=HTMLResponse)
async def plans_db_page(request: Request):
    """Просмотр БД планов адаптации: структура (plans) и расписания (plan_schedules) в JSONB."""
    return page_for_admin(request, "plans_db.html")


@router.get("/doc-breakdown", response_class=HTMLResponse)
async def doc_breakdown_page(request: Request):
    """Страница просмотра разбора документа (блоки → чанки, метки, обоснования)."""
    return page_for_admin(request, "doc_breakdown.html")
