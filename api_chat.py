"""Чат с ассистентом: вопрос -> RAG -> ответ, память диалога, эскалация человеку."""

from fastapi import APIRouter, Depends, HTTPException
import time
import uuid
import threading
from collections import OrderedDict, deque

from pydantic import BaseModel

import questions
from rag import handle_question, HISTORY_WINDOW
from deps import require_setup_done, logged_in

router = APIRouter()


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


def _current_stage_ids(user: dict) -> list:
    """Текущий этап обучения пользователя (ТЗ §6) — приоритет поиска, не фильтр.
    ponytail: пока прогресс обучения по этапам-блокам отдельно не трекается, отдаём
    пусто (поиск работает без буста). Точка интеграции, когда появится прогресс:
    вернуть id этапов из stages, на которых сейчас пользователь."""
    return []


# ---------- RAG-вопросы ----------
ESCALATE_REPLY = ("⚠️ Вопрос требует внимания специалиста — передал его ответственному. "
                  "Ответ придёт в личный кабинет.")
NO_ANSWER_REPLY = ("В регламентах точного ответа не нашлось — передал вопрос ответственному. "
                   "Ответ придёт в личный кабинет.")


def _route_to_human(result: dict, user: dict) -> dict:
    """
    Вопрос без ответа не теряем: ставим в очередь администратору и показываем
    сотруднику понятное сообщение вместо пустого ответа/технической ошибки.
    Срабатывает для escalate (ЧС) и для rag без найденного ответа. ЧС по регэкспу
    (result["emergency"]) уже несёт инструкцию — её сохраняем, но вопрос всё равно
    ставим в очередь человеку.
    """
    emergency = result.get("emergency")
    if not emergency and (result.get("answer") or result.get("route") not in ("rag", "escalate")):
        return result
    cls = result.get("classification") or {}
    reason = questions.REASON_ESCALATE if (result["route"] == "escalate" or cls.get("risk_flag")) \
        else questions.REASON_NO_ANSWER
    questions.record(user, result["question"], result.get("resolved_question"),
                     reason, cls.get("risk_type"))
    result["escalated"] = True
    result["error"] = None  # «нет кандидатов» — не ошибка для пользователя, это эскалация
    if not result.get("answer"):
        result["answer"] = ESCALATE_REPLY if reason == questions.REASON_ESCALATE else NO_ANSWER_REPLY
    return result


# Синхронный def (не async): handle_question ходит в Ollama/Qdrant синхронными
# requests на секунды-минуты. В async-обработчике это заблокировало бы весь event loop
# uvicorn-воркера — «зависли» бы все параллельные запросы. Обычный def FastAPI выполняет
# в threadpool, поэтому воркер продолжает обслуживать других пользователей.
@router.post("/ask", response_model=QuestionResponse, dependencies=logged_in)
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
        result = handle_question(question, history=history, current_stage_ids=_current_stage_ids(user),
                                 position=user.get("position"))
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


@router.delete("/session/{session_id}")
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
