import time
import json
import uuid
import threading
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from rag import handle_question, HISTORY_WINDOW
from config import MAX_UPLOAD_BYTES
import db
import indexing
import auth
import users
import questions
import folders
import classify


def _bg(fn, *args):
    """Фоновая задача (реанализ базы и т.п. — может быть долгой из-за LLM)."""
    threading.Thread(target=fn, args=args, daemon=True).start()


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Схема БД (PostgreSQL) — до первого обращения к аккаунтам
    db.init_schema()
    # Стартовая структура знаний (смысловые папки) из data/knowledge_seed.json —
    # только если таблица пуста. Дальше папками управляет человек.
    seed_path = BASE_DIR / "data" / "knowledge_seed.json"
    if not seed_path.exists():
        print(f"ВНИМАНИЕ: нет файла сида {seed_path} — стартовые папки не заведены.")
    else:
        try:
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
            f = folders.seed_if_empty(seed.get("folders", []))
            print(f"Стартовая структура знаний: засеяно папок {f} "
                  f"(в БД сейчас: папок {len(folders.list_folders())}).")
        except Exception as e:
            # Не роняем старт из-за сида — папки можно засеять `python seed_knowledge.py`.
            print(f"ОШИБКА посева стартовой структуры: {e}")
    # Векторная коллекция — до первого /ask или загрузки файла
    indexing.create_collection(recreate=False)
    # Векторы папок для классификации документов (перестраиваются при изменениях).
    try:
        classify.sync_folder_vectors()
    except Exception as e:
        print(f"Предупреждение: векторы папок не построены (Qdrant/Ollama?): {e}")

    # Разовая миграция со старого файлового хранилища аккаунтов в БД
    moved_json = users.migrate_legacy_json_users()
    if moved_json:
        print(f"Перенесено аккаунтов из users.json в БД: {moved_json}")

    initial = users.ensure_owner()
    if initial:
        print("=" * 70)
        print("Создана учётная запись главного администратора.")
        print(f"  Логин:  {initial['username']}")
        print(f"  Пароль: {initial['password']}")
        print(f"  Дубль записан в {users.INITIAL_CREDENTIALS_PATH}")
        print("  При первом входе система попросит задать свои логин и пароль.")
        print("=" * 70)
    yield


app = FastAPI(title="RAG Assistant API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class QuestionRequest(BaseModel):
    question: str
    session_id: str | None = None   # если не передан, сервер создаст новый


class QuestionResponse(BaseModel):
    question: str
    resolved_question: str | None = None   # вопрос, переформулированный с учётом истории (если применимо)
    context_used: bool = False             # был ли использован контекст предыдущих вопросов
    session_id: str
    classification: dict
    route: str
    candidates: list
    top_fragments: list
    answer: str | None = None
    escalated: bool = False                 # вопрос без ответа передан администратору
    elapsed_time: float
    error: str | None = None


# ---------- Память диалога по сессиям (in-memory) ----------
# Хранит последние вопросы/ответы для каждого session_id — используется, чтобы
# handle_question мог разрешать контекстные вопросы вроде "Где взять".
# Живёт только в памяти текущего процесса: подходит для одного uvicorn-воркера;
# для многопроцессного/многосерверного деплоя нужно вынести в Redis или аналог.
HISTORY_MAX_STORE = 20  # сколько реплик хранить на сессию (окно анализа контекста меньше — HISTORY_WINDOW)
# Верхняя граница числа сессий в памяти. Без неё словарь рос бы бесконечно (каждая
# новая вкладка = новый session_id), медленно утекая по памяти. При переполнении
# вытесняется самая давно не активная сессия (LRU) — её история просто пересоздастся.
MAX_SESSIONS = 5000

_history_lock = threading.Lock()
_conversation_history: "OrderedDict[str, deque]" = OrderedDict()
_session_owner: dict[str, str] = {}   # session_id -> user_id первого владельца сессии


def _evict_sessions_locked():
    """Держим не больше MAX_SESSIONS сессий. Вызывать под _history_lock."""
    while len(_conversation_history) > MAX_SESSIONS:
        old_sid, _ = _conversation_history.popitem(last=False)
        _session_owner.pop(old_sid, None)


def get_recent_history(session_id: str, n: int = HISTORY_WINDOW) -> list:
    with _history_lock:
        hist = list(_conversation_history.get(session_id, []))
    return hist[-n:]


def append_history(session_id: str, question: str, answer: str | None, owner_id: str | None = None):
    with _history_lock:
        dq = _conversation_history.get(session_id)
        if dq is None:
            dq = deque(maxlen=HISTORY_MAX_STORE)
            _conversation_history[session_id] = dq
        else:
            _conversation_history.move_to_end(session_id)   # активная сессия — в конец очереди LRU
        dq.append({"question": question, "answer": answer})
        if owner_id and session_id not in _session_owner:
            _session_owner[session_id] = owner_id
        _evict_sessions_locked()


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


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Личный кабинет сотрудника: чат с ассистентом по регламентам."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_credentials"):
        return RedirectResponse(url="/setup", status_code=303)
    return HTMLResponse(_read_static("index.html"))


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _read_static("login.html")


@app.get("/register", response_class=HTMLResponse)
async def register_page():
    return _read_static("register.html")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Первичная настройка: замена выданных логина и пароля своими."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not user.get("must_change_credentials"):
        return RedirectResponse(url="/admin" if users.is_admin(user) else "/", status_code=303)
    return HTMLResponse(_read_static("setup.html"))


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Админка: база знаний (документы, смысловые папки), пользователи, эскалации."""
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.get("must_change_credentials"):
        return RedirectResponse(url="/setup", status_code=303)
    if not users.is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse(_read_static("admin.html"))


# ---------- Вход, регистрация, свой профиль ----------
@app.post("/api/login")
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


@app.post("/api/register")
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


@app.post("/api/logout")
async def api_logout(request: Request, response: Response):
    auth.logout(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"logged_out": True}


@app.get("/api/me")
async def api_me(user: dict = Depends(current_user)):
    return users.public_view(user)


@app.post("/api/setup-credentials")
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


@app.post("/api/password")
async def api_change_password(req: PasswordChangeRequest, response: Response,
                              user: dict = Depends(current_user)):
    try:
        auth.change_own_password(user, req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Смена пароля разлогинивает все сессии, включая текущую
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"changed": True}


@app.get("/api/my/questions", dependencies=logged_in)
async def api_my_questions(user: dict = Depends(require_setup_done)):
    """Свои эскалированные вопросы и ответы на них от администратора."""
    return {"questions": questions.list_for_user(user["id"])}


# ---------- RAG-вопросы ----------
ESCALATE_REPLY = ("⚠️ Вопрос требует внимания специалиста — передал его ответственному. "
                  "Ответ придёт в личный кабинет.")
NO_ANSWER_REPLY = ("В регламентах точного ответа не нашлось — передал вопрос ответственному. "
                   "Ответ придёт в личный кабинет.")


def _route_to_human(result: dict, user: dict) -> dict:
    """
    Вопрос без ответа не теряем: ставим в очередь администратору и показываем
    сотруднику понятное сообщение вместо пустого ответа/технической ошибки.
    Срабатывает для escalate (ЧС) и для rag без найденного ответа.
    """
    if result.get("answer") or result.get("route") not in ("rag", "escalate"):
        return result
    cls = result.get("classification") or {}
    reason = questions.REASON_ESCALATE if (result["route"] == "escalate" or cls.get("risk_flag")) \
        else questions.REASON_NO_ANSWER
    questions.record(user, result["question"], result.get("resolved_question"),
                     reason, cls.get("risk_type"))
    result["escalated"] = True
    result["error"] = None  # «нет кандидатов» — не ошибка для пользователя, это эскалация
    result["answer"] = ESCALATE_REPLY if reason == questions.REASON_ESCALATE else NO_ANSWER_REPLY
    return result


# Синхронный def (не async): handle_question ходит в Ollama/Qdrant синхронными
# requests на секунды-минуты. В async-обработчике это заблокировало бы весь event loop
# uvicorn-воркера — «зависли» бы все параллельные запросы. Обычный def FastAPI выполняет
# в threadpool, поэтому воркер продолжает обслуживать других пользователей.
@app.post("/ask", response_model=QuestionResponse, dependencies=logged_in)
def ask(req: QuestionRequest, user: dict = Depends(require_setup_done)):
    start = time.time()
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Вопрос не может быть пустым")

    # session_id связывает подряд идущие вопросы в один диалог. Фронтенд генерирует
    # его один раз на вкладку и присылает с каждым запросом; если его нет — заводим новый.
    session_id = req.session_id or str(uuid.uuid4())
    history = get_recent_history(session_id)

    try:
        result = handle_question(question, history=history, position=user.get("position"))
        result = _route_to_human(result, user)
        result["elapsed_time"] = time.time() - start
        result["session_id"] = session_id
        append_history(session_id, question, result.get("answer"), owner_id=user["id"])
        return QuestionResponse(**result)
    except Exception as e:
        # Внутреннюю причину — только в лог сервера, наружу общее сообщение:
        # str(e) может раскрывать детали инфраструктуры (адреса, схемы, стек).
        print(f"[ASK] ошибка обработки вопроса (session={session_id}): {e!r}")
        return QuestionResponse(
            question=question,
            session_id=session_id,
            classification={},
            route="error",
            candidates=[],
            top_fragments=[],
            answer=None,
            elapsed_time=time.time() - start,
            error="Не удалось обработать вопрос. Попробуйте ещё раз позже."
        )


@app.delete("/session/{session_id}")
async def reset_session(session_id: str, user: dict = Depends(require_setup_done)):
    """Очищает историю диалога для сессии (например, при нажатии «Новый диалог» на сайте).
    Чужую сессию чистить нельзя — иначе любой вошедший стирал бы историю по чужому id."""
    with _history_lock:
        owner = _session_owner.get(session_id)
        if owner and owner != user["id"]:
            raise HTTPException(status_code=403, detail="Это не ваша сессия")
        existed = session_id in _conversation_history
        _conversation_history.pop(session_id, None)
        _session_owner.pop(session_id, None)
    return {"session_id": session_id, "cleared": existed}


# ---------- Управление документами ----------
@app.get("/documents", dependencies=admin_only)
async def get_documents():
    """Список документов в базе с их статусом индексации (загружен / обрабатывается / готов / ошибка)."""
    return {"documents": indexing.list_documents()}


@app.post("/documents/{filename}/reindex", dependencies=admin_only)
async def reindex_document(filename: str):
    """Переанализ документа (без повторного docling): обновляет папки по чанкам."""
    result = indexing.reanalyze_document(filename)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/documents/upload", dependencies=admin_only)
async def upload_document(file: UploadFile = File(...)):
    """
    Загрузка нового регламента. Файл сохраняется в data/documents/ и ставится
    в фоновую очередь на индексацию (docling -> чанкинг -> эмбеддинги -> Qdrant).
    Ответ приходит сразу (202) с идентификатором задачи; прогресс —
    через GET /documents/jobs/{job_id}. Тяжёлый разбор PDF не держит запрос.
    """
    # Читаем не больше лимита +1 байт: иначе гигабайтный файл целиком буферизуется в
    # RAM ещё до проверки размера (потенциальный OOM). Лишний байт нужен, чтобы отличить
    # «ровно лимит» от «больше лимита».
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")

    try:
        filepath = indexing.save_uploaded_file(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = indexing.enqueue_document(filepath)
    return JSONResponse(status_code=202, content=job)


@app.get("/documents/jobs/{job_id}", dependencies=admin_only)
async def get_document_job(job_id: str):
    """Статус фоновой индексации загруженного документа."""
    job = indexing.get_index_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job


@app.get("/documents/{filename}/chunks", dependencies=admin_only)
async def get_document_chunks(filename: str):
    """Подробности разбиения документа: чанки и вектор каждого чанка (для кнопки «Подробнее»)."""
    detail = indexing.get_document_chunks(filename)
    if detail is None:
        raise HTTPException(status_code=404, detail="Чанки не найдены — документ ещё не проиндексирован")
    return detail


@app.delete("/documents/{filename}", dependencies=admin_only)
async def remove_document(filename: str):
    """Удаляет документ: векторы из Qdrant, оригинал из data/documents, кэш docling."""
    existed = indexing.delete_document(filename)
    if not existed:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {"filename": filename, "deleted": True}


# ---------- Смысловые папки (логические категории; ими управляет человек) ----------
class FolderRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    criteria: list | None = None
    stage_ids: list | None = None
    enabled: bool | None = None


@app.get("/folders", dependencies=admin_only)
async def get_folders():
    """Смысловые папки базы знаний. Их создаёт и редактирует человек — ИИ только
    классифицирует документы внутрь существующих папок, но не заводит новые."""
    result = folders.list_folders()
    try:
        counts = indexing.folder_doc_counts()
    except Exception:
        counts = {}   # счётчик документов не должен ронять список папок
    for f in result:
        f["documents"] = counts.get(f["slug"], 0)
    return {"folders": result}


@app.get("/folders/{slug}/chunks", dependencies=admin_only)
async def get_folder_chunks(slug: str):
    """Чанки внутри смысловой папки — просмотр содержимого папки (текст + из какого документа)."""
    return indexing.get_folder_chunks(slug)


@app.post("/folders", dependencies=admin_only)
def create_folder(req: FolderRequest):
    try:
        folder = folders.create_folder(req.name, req.description or "", req.criteria, req.stage_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Новая папка -> обновляем векторы и переанализируем потенциально релевантные
    # документы, чтобы они попали в неё.
    classify.sync_folder_vectors()
    _bg(indexing.reanalyze_for_folder, folder["slug"])
    return folder


@app.put("/folders/{folder_id}", dependencies=admin_only)
def update_folder(folder_id: str, req: FolderRequest):
    existing = folders.get_folder(folder_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    try:
        folder = folders.update_folder(folder_id, **req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    classify.sync_folder_vectors()
    # Изменились критерии/название или папку включили — переанализируем документы.
    fields = req.model_dump(exclude_none=True)
    if any(k in fields for k in ("name", "description", "criteria", "stage_ids", "enabled")):
        _bg(indexing.reanalyze_for_folder, folder["slug"])
    return folder


@app.delete("/folders/{folder_id}", dependencies=admin_only)
def delete_folder(folder_id: str):
    """Удаляет только логическую категорию — документы остаются в общей базе знаний."""
    folder = folders.get_folder(folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    folders.delete_folder(folder_id)
    indexing.strip_folder_from_chunks(folder["slug"])   # снимаем метку с чанков, документы не трогаем
    classify.sync_folder_vectors()
    return {"deleted": True}


# ---------- Повторный анализ и уточнения по документам ----------
class ClarifyRequest(BaseModel):
    clarification: str


@app.post("/documents/reanalyze", dependencies=admin_only)
def reanalyze_documents():
    """Полный повторный анализ всей базы под текущую структуру папок (фоново)."""
    classify.sync_folder_vectors()
    _bg(indexing.reanalyze_all)
    return {"started": True}


@app.post("/documents/{filename}/reanalyze", dependencies=admin_only)
def reanalyze_one(filename: str):
    """Переанализ одного документа — фоново; статус (reanalyzing -> indexed/error)
    виден в списке документов рядом с этим документом."""
    _bg(indexing.reanalyze_document, filename)
    return {"started": True}


@app.post("/documents/{filename}/clarify", dependencies=admin_only)
async def clarify_document(filename: str, req: ClarifyRequest):
    """Текстовое уточнение пользователя (актуальность/архив/область действия).
    Исходный документ не переписывается — уточнение хранится как доп. контекст."""
    if not indexing.set_clarification(filename, req.clarification):
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {"filename": filename, "clarification": req.clarification}


# ---------- Вопросы без ответа (эскалация человеку) ----------
class ResolveRequest(BaseModel):
    answer: str


@app.get("/questions", dependencies=admin_only)
async def get_questions(status: str | None = "open"):
    """Очередь вопросов сотрудников, на которые ассистент не ответил сам."""
    return {"questions": questions.list_all(status=status or None),
            "open_count": questions.count_open()}


@app.post("/questions/{qid}/resolve", dependencies=admin_only)
async def resolve_question(qid: str, req: ResolveRequest, actor: dict = Depends(require_admin)):
    """Администратор отвечает на вопрос — ответ уходит в личный кабинет сотрудника."""
    try:
        entry = questions.resolve(qid, req.answer, actor.get("full_name") or actor.get("username"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if entry is None:
        raise HTTPException(status_code=404, detail="Вопрос не найден")
    return entry


# ---------- Пользователи и роли ----------
class UserRequest(BaseModel):
    full_name: str
    username: str | None = None
    password: str | None = None
    position: str | None = None
    department: str | None = None
    contact: str | None = None


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


@app.get("/users", dependencies=admin_only)
async def get_users():
    result = []
    for user in users.list_users():
        result.append({**user, "has_account": bool(user.get("username"))})
    return {"users": result}


@app.post("/users", dependencies=admin_only)
async def create_user(req: UserRequest, actor: dict = Depends(require_admin)):
    """
    Заведение сотрудника администратором. Логин и временный пароль необязательны:
    профиль можно создать заранее, а доступ выдать позже.
    """
    try:
        user = users.create_user(req.model_dump(), actor=actor, role=users.ROLE_EMPLOYEE,
                                 must_change_credentials=bool(req.password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return users.public_view(user)


@app.get("/users/{user_id}", dependencies=admin_only)
async def get_user(user_id: str):
    user = users.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return users.public_view(user)


@app.put("/users/{user_id}")
async def update_user(user_id: str, req: UserRequest, actor: dict = Depends(require_admin)):
    """Правка профиля сотрудника."""
    _target_user(user_id, actor)
    user = users.update_profile(user_id, req.model_dump())
    return users.public_view(user)


@app.delete("/users/{user_id}", dependencies=owner_only)
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


@app.post("/users/{user_id}/role", dependencies=owner_only)
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


@app.post("/users/{user_id}/active")
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


@app.post("/users/{user_id}/credentials")
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


@app.post("/users/{user_id}/transfer-ownership", dependencies=owner_only)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
