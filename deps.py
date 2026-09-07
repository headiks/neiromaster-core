"""
Общие зависимости веб-слоя: кто вошёл, что ему можно видеть и менять.

Здесь живут проверки доступа (current_user -> require_setup_done -> require_admin /
require_owner) и правила разграничения между администраторами. Роутеры api_*.py
импортируют их отсюда, а не заводят свои копии, — иначе правило «администратор
видит только своё» пришлось бы поддерживать в нескольких местах.
"""

import threading
from pathlib import Path

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import auth
import users
import indexing

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _bg(fn, *args):
    """Фоновая задача (реанализ базы и т.п. — может быть долгой из-за LLM)."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def _read_static(name: str) -> str:
    with open(STATIC_DIR / name, "r", encoding="utf-8") as f:
        return f.read()


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        auth.COOKIE_NAME, token,
        httponly=True,          # кука недоступна из JS — защита от кражи через XSS
        samesite="lax",         # браузер не пришлёт её при кросс-сайтовых POST — базовая защита от CSRF
        secure=auth.COOKIE_SECURE,  # только по HTTPS — токен не утечёт по чистому HTTP (MITM)
        max_age=auth.SESSION_TTL,
        path="/",
    )


# ---------- Доступ ----------
def current_user(request: Request) -> dict:
    """Любой вошедший пользователь. Без валидной сессии — 401."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


def require_setup_done(user: dict = Depends(current_user)) -> dict:
    """
    Пока главный администратор не задал свои логин и пароль, дальше первичной
    настройки его не пускаем — иначе сгенерированные данные так и останутся жить.
    """
    if user.get("must_change_credentials"):
        raise HTTPException(status_code=403, detail="Сначала задайте свои логин и пароль")
    return user


def require_admin(user: dict = Depends(require_setup_done)) -> dict:
    if not users.is_admin(user):
        raise HTTPException(status_code=403, detail="Доступ только для администраторов")
    return user


def require_owner(user: dict = Depends(require_setup_done)) -> dict:
    if not users.is_owner(user):
        raise HTTPException(status_code=403,
                            detail="Это может сделать только главный администратор")
    return user


logged_in = [Depends(require_setup_done)]
admin_only = [Depends(require_admin)]
owner_only = [Depends(require_owner)]


# ---------- Разграничение видимости между администраторами ----------
# Суперадмин (owner) видит всё, что загрузили администраторы, и всех людей.
# Администратор — только СВОИ загруженные документы и людей СВОЕГО отдела.
# Сотрудник (employee) сюда не попадает: админские ручки закрыты require_admin.
can_see_doc = users.can_see_doc          # правила — в users.py (там же и тесты)


def visible_documents(user: dict) -> list:
    return users.visible_docs(user, indexing.list_documents())


def ensure_doc_access(user: dict, filename: str):
    """403, если администратор обращается к чужому документу. Незнакомое имя
    пропускаем — свой 404 отдаст сам обработчик."""
    if users.is_owner(user):
        return
    doc = next((d for d in indexing.list_documents() if d.get("filename") == filename), None)
    if doc is not None and not can_see_doc(user, doc):
        raise HTTPException(status_code=403,
                            detail="Документ загружен другим администратором")


def page_for_admin(request: Request, filename: str):
    """
    Страница админки: вошёл -> прошёл первичную настройку -> администратор.
    Один хелпер вместо пяти одинаковых блоков в маршрутах страниц.
    """
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_credentials"):
        return RedirectResponse(url="/setup", status_code=303)
    if not users.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse(_read_static(filename))
