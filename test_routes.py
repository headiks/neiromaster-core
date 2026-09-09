"""
Проверка сборки приложения из роутеров (app.py + api_*.py).

После разнесения маршрутов по модулям главный риск — потерять маршрут или,
что хуже, потерять на нём проверку доступа. Тест собирает настоящее приложение
и проверяет инварианты:

  * все ожидаемые пути на месте и ни один не задвоился;
  * каждый непубличный маршрут требует входа (зависимость или Depends в сигнатуре);
  * страницы админки и ручки документов не открыты анонимам.

Нужны установленные зависимости (fastapi и т.д.) — на машине без них тест
сообщает SKIP, а не падает. Запуск: python test_routes.py
"""

import sys

# Публичные маршруты: вход, регистрация и статика — им проверка доступа не нужна.
PUBLIC = {"/login", "/register", "/api/login", "/api/register", "/api/logout", "/static"}
# Служебные маршруты самого FastAPI (схема и Swagger). ВНИМАНИЕ: они открыты
# анонимам и показывают полный список ручек; чтобы закрыть — FastAPI(docs_url=None,
# redoc_url=None, openapi_url=None) в app.py.
FRAMEWORK = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
# Маршруты, которые сами решают, куда перенаправить неавторизованного.
SELF_GUARDED = {"/", "/setup", "/api/me", "/api/setup-credentials", "/api/password"}

EXPECTED_SAMPLE = [
    "/", "/login", "/admin", "/s3", "/doc-breakdown", "/documents-board", "/documents-table", "/logs",
    "/api/login", "/api/me", "/api/s3/list", "/ask", "/documents", "/documents/upload",
    "/documents/board", "/documents/table", "/documents/labeled", "/documents/{filename}",
    "/folders", "/knowledge/stages", "/questions", "/catalog", "/plans", "/users",
    "/users/{user_id}", "/users/delete-non-admins", "/staffing/import", "/jobs/{job_id}",
]


def load_app():
    import app
    return app.app


def routes(application):
    """
    Плоский список маршрутов приложения.

    С FastAPI 0.141 include_router кладёт в app.routes не сами маршруты, а обёртку
    _IncludedRouter (маршруты лежат в её original_router) — поэтому разворачиваем
    вложенные роутеры рекурсивно, иначе увидим только служебные /docs и /static.
    """
    out = []
    stack = list(application.routes)
    while stack:
        r = stack.pop(0)
        inner = getattr(r, "original_router", None) or (
            getattr(r, "router", None) if not hasattr(r, "path") else None)
        if inner is not None:
            stack.extend(inner.routes)
        elif getattr(r, "path", None):
            out.append(r)
    return out


def test_all_expected_paths_present():
    paths = {r.path for r in routes(load_app())}
    missing = [p for p in EXPECTED_SAMPLE if p not in paths]
    assert not missing, f"маршруты потерялись при разнесении по роутерам: {missing}"


def test_no_duplicate_path_methods():
    seen = set()
    dupes = []
    for r in routes(load_app()):
        for method in sorted(getattr(r, "methods", None) or []):
            key = (method, r.path)
            if key in seen:
                dupes.append(key)
            seen.add(key)
    assert not dupes, f"один и тот же маршрут объявлен дважды: {dupes}"


def test_every_private_route_requires_login():
    """Ни одна непубличная ручка не должна отвечать анониму."""
    import inspect
    unguarded = []
    for r in routes(load_app()):
        if (r.path in PUBLIC or r.path in SELF_GUARDED or r.path in FRAMEWORK
                or r.path.startswith("/static")):
            continue
        endpoint = getattr(r, "endpoint", None)
        if endpoint is None:
            continue
        # проверка либо в dependencies маршрута, либо в аргументах-Depends обработчика
        guarded = bool(getattr(r, "dependencies", None))
        if not guarded:
            src = str(inspect.signature(endpoint))
            guarded = any(name in src for name in
                          ("require_admin", "require_owner", "require_setup_done", "current_user"))
        if not guarded:
            # страницы отдают редирект на /login внутри обработчика
            guarded = "page_for_admin" in (inspect.getsource(endpoint) or "")
        if not guarded:
            unguarded.append(f"{sorted(r.methods or [])} {r.path}")
    assert not unguarded, f"маршруты без проверки доступа: {unguarded}"


def test_admin_pages_are_admin_only():
    import inspect
    for r in routes(load_app()):
        if r.path in ("/admin", "/s3", "/documents-board", "/documents-table", "/doc-breakdown", "/logs"):
            assert "page_for_admin" in inspect.getsource(r.endpoint), \
                f"страница {r.path} не закрыта page_for_admin"


if __name__ == "__main__":
    try:
        load_app()
    except ImportError as e:
        print(f"SKIP test_routes: нет зависимостей приложения ({e})")
        sys.exit(0)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK ", name)
    print(f"test_routes: маршрутов {len(routes(load_app()))} — все проверки пройдены")
