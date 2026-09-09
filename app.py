"""
Точка входа приложения: сборка FastAPI из роутеров и разовые действия при старте.

Маршруты живут в api_*.py (страницы, аккаунты, чат, документы, знания, планы, люди),
общие проверки доступа — в deps.py. Здесь остаётся только то, что касается
приложения целиком: инициализация схемы БД и векторов, миграции со старых форматов,
подключение роутеров.

ВАЖНО: порядок include_router повторяет прежний порядок объявления маршрутов —
FastAPI выбирает первый подходящий шаблон, и переставлять роутеры нельзя.
"""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn

import db
import auth
import users
import folders
import stages
import classify
import indexing
import documents
import messaging
import activitylog
from deps import BASE_DIR, STATIC_DIR

import api_pages
import api_accounts
import api_chat
import api_documents
import api_knowledge
import api_plans
import api_people
import api_activity

ROUTERS = (api_pages, api_accounts, api_chat, api_documents,
           api_knowledge, api_plans, api_people, api_activity)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Схема БД (PostgreSQL) — до первого обращения к аккаунтам
    db.init_schema()
    # Стартовая структура знаний (этапы + смысловые папки) из data/knowledge_seed.json —
    # только если таблицы пусты. Получена из исходного Excel; дальше ей управляет человек.
    seed_path = BASE_DIR / "data" / "knowledge_seed.json"
    if not seed_path.exists():
        print(f"ВНИМАНИЕ: нет файла сида {seed_path} — стартовые папки не заведены.")
    else:
        try:
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
            s = stages.seed_if_empty(seed.get("stages", []))
            f = folders.seed_if_empty(seed.get("folders", []))
            print(f"Стартовая структура знаний: засеяно этапов {s}, папок {f} "
                  f"(в БД сейчас: папок {len(folders.list_folders())}).")
        except Exception as e:
            # Не роняем старт из-за сида — логируем, папки можно засеять `python seed_knowledge.py`.
            print(f"ОШИБКА посева стартовой структуры: {e}")
    # Векторная коллекция — до первого /ask или загрузки файла
    indexing.create_collection(recreate=False)
    # Векторы папок для классификации документов (перестраиваются при изменениях).
    try:
        classify.sync_folder_vectors()
    except Exception as e:
        print(f"Предупреждение: векторы папок не построены (Qdrant/Ollama?): {e}")

    # Пайплайн разметки docpipe: таблицы PG + версия плана из каталога адаптации.
    try:
        import docpipe
        docpipe.init_schema()
        docpipe.sync_plan_from_catalog()
    except Exception as e:
        print(f"Предупреждение: пайплайн разметки docpipe не инициализирован: {e}")

    # Разовые миграции со старых файловых хранилищ в БД
    moved_json = users.migrate_legacy_json_users()
    if moved_json:
        print(f"Перенесено аккаунтов из users.json в БД: {moved_json}")
    moved = users.migrate_legacy_employees()
    if moved:
        print(f"Перенесено записей сотрудников из employees.json в БД: {moved}")

    initial = users.ensure_owner()
    if initial:
        print("=" * 70)
        print("Создана учётная запись главного администратора.")
        print(f"  Логин:  {initial['username']}")
        print(f"  Пароль: {initial['password']}")
        print(f"  Дубль записан в {users.INITIAL_CREDENTIALS_PATH}")
        print("  При первом входе система попросит задать свои логин и пароль.")
        print("=" * 70)

    # Единый реестр метаданных документов (PostgreSQL): дедуп по хэшу + экран
    # «этапы ↔ документы». Таблица создаётся, если её ещё нет.
    try:
        documents.init()
    except Exception as e:
        print(f"Предупреждение: реестр документов не инициализирован: {e}")

    # Фоновый планировщик доставки сообщений плана по расписанию (инбокс сотрудника).
    # Отключается NEIROMASTER_SCHEDULER=0 (напр. когда доставку гоняют внешним cron).
    messaging.start_scheduler()
    yield


# docs_url/redoc_url/openapi_url=None: служебные страницы FastAPI (Swagger и схема)
# открыты анониму и раскрывают полный список ручек — на проде не нужны.
app = FastAPI(title="RAG Assistant API", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def log_page_views(request, call_next):
    """Просмотры страниц — централизованно: GET-запрос, отдавший HTML (не /static, не /api).
    Так не нужно дублировать логирование в каждом обработчике страницы."""
    response = await call_next(request)
    try:
        path = request.url.path
        if (request.method == "GET" and response.status_code == 200
                and not path.startswith("/static") and not path.startswith("/api")
                and "text/html" in response.headers.get("content-type", "")):
            user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
            activitylog.log("page_view", user=user, request=request, path=path)
    except Exception:
        pass
    return response


for _module in ROUTERS:
    app.include_router(_module.router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
