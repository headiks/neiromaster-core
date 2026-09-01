import os
import requests
import json
import re
import time

import classify
import folders
from config import OLLAMA_URL as OLLAMA, QDRANT_HOST, QDRANT_PORT, get_embedding as _get_embedding

# ---------- Конфигурация ----------
QDRANT = f"http://{QDRANT_HOST}:{QDRANT_PORT}"   # хост/порт Qdrant — из config (единый источник)
COLLECTION = "reglaments"
SMALL_MODEL = "qwen2.5:3b"      # для скорости, но можно поставить 7b для точности
BIG_MODEL = "qwen3:14b"
CONFIDENCE_THRESHOLD = 0.55
MAX_CONTEXT_FRAGMENTS = 3
HISTORY_WINDOW = 3   # сколько последних вопросов пользователя учитывать при разрешении контекста
# Подробный лог пайплайна: промпты, сырые ответы моделей, тексты вопросов. По умолчанию
# ВЫКЛ — в проде он шумный и может писать в логи чувствительные данные (содержимое
# регламентов, вопросы сотрудников). Включается переменной NEIROMASTER_DEBUG=1.
DEBUG = os.environ.get("NEIROMASTER_DEBUG", "").lower() in ("1", "true", "yes")

SMALL_LLM_TIMEOUT = 60
BIG_LLM_TIMEOUT = 90
QDRANT_TIMEOUT = 15

# ---------- Быстрый префильтр для общих фраз и ключевых слов ----------
GREETING_PHRASES = [
    "привет", "здравствуй", "здравствуйте", "добрый день",
    "доброе утро", "добрый вечер", "как дела", "как жизнь",
    "спасибо", "благодарю", "ок", "хорошо", "понял", "да", "нет"
]

# Ключевые слова, которые однозначно указывают на RAG-запрос
RAG_KEYWORDS = [
    "отпуск", "высота", "инструктаж", "техника безопасности", "охрана труда",
    "смена", "регламент", "правила", "норма", "требование", "обязан",
    "положено", "разрешается", "запрещается", "инструкция", "порядок"
]

def is_greeting_or_general(text):
    text_lower = text.lower().strip()
    if '?' in text_lower:
        return False
    question_starters = ["что", "как", "где", "когда", "почему", "зачем", "сколько", "кто", "какой"]
    if any(text_lower.startswith(w) for w in question_starters):
        return False
    words = re.findall(r'\w+', text_lower)
    if not words or len(words) > 4:
        return False

    # Многословное приветствие как самостоятельная фраза («как дела», «добрый день»).
    for phrase in GREETING_PHRASES:
        if ' ' in phrase and re.search(r'\b' + re.escape(phrase) + r'\b', text_lower):
            return True

    # Однословные приветствия и подтверждения («да», «нет», «ок», «спасибо») считаем
    # «общей фразой» ТОЛЬКО когда из них состоит ВЕСЬ текст. Иначе «нет» в «мне плохо
    # нет сил» или «да» в «станок сломался да искрит» ошибочно увели бы реальное — и
    # даже тревожное — сообщение в маршрут general мимо RAG и эскалации.
    single = {p for p in GREETING_PHRASES if ' ' not in p}
    if all(w in single for w in words):
        return True
    return False

def has_rag_keywords(text):
    text_lower = text.lower()
    for kw in RAG_KEYWORDS:
        if kw in text_lower:
            return True
    return False

# ---------- Вспомогательные функции ----------
def log(step, msg, data=None):
    if not DEBUG:
        return
    print(f"[{step}] {msg}")
    if data is not None:
        print(f"    {data}")

def embed(text):
    # Единая точка эмбеддинга — config.get_embedding (та же модель bge-m3 и таймаут,
    # что и у индексации), чтобы не держать вторую копию модели/таймаута/эндпоинта.
    log("EMBED", f"Запрос эмбеддинга для текста: {text[:50]}...")
    start = time.time()
    result = _get_embedding(text)
    log("EMBED", f"Эмбеддинг получен за {time.time()-start:.3f} сек, размерность {len(result)}")
    return result

def small_llm(system, user, step_name="SMALL_LLM"):
    log(step_name, f"Запрос к малой модели:\n  system={system[:80]}...\n  user={user[:80]}...")
    start = time.time()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": SMALL_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }, timeout=SMALL_LLM_TIMEOUT)
    r.raise_for_status()
    response = r.json()["message"]["content"]
    elapsed = time.time() - start
    log(step_name, f"Ответ получен за {elapsed:.3f} сек, длина {len(response)} символов")
    log(step_name, f"Сырой ответ: {response}")
    return response

def big_llm(system, user):
    log("BIG_LLM", f"Запрос к большой модели:\n  system={system[:80]}...\n  user={user[:80]}...")
    start = time.time()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": BIG_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "options": {"num_keep": 0}
    }, timeout=BIG_LLM_TIMEOUT)
    r.raise_for_status()
    response = r.json()["message"]["content"]
    elapsed = time.time() - start
    log("BIG_LLM", f"Ответ получен за {elapsed:.3f} сек, длина {len(response)} символов")
    log("BIG_LLM", f"Сырой ответ: {response}")
    return response

def parse_json_response(text):
    log("PARSE", f"Попытка извлечь JSON из: {text[:100]}...")
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```', '', text)
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        raise ValueError("В ответе нет JSON-объекта")
    json_str = text[start:end+1]
    try:
        parsed = json.loads(json_str)
        log("PARSE", f"JSON успешно распарсен: {parsed}")
        return parsed
    except json.JSONDecodeError as e:
        log("PARSE", f"Ошибка парсинга JSON: {e}")
        raise

# ---------- Анализ истории диалога (разрешение контекстных вопросов) ----------
# Если пользователь до этого спрашивал про "работу на высоте" и "какая экипировка нужна",
# а затем задаёт короткий обрывочный вопрос "Где взять" — сам по себе он ни классификатору,
# ни поиску, ни генератору ответа не даст ничего осмысленного. Этот блок смотрит на последние
# HISTORY_WINDOW вопросов пользователя и, если текущий вопрос зависит от контекста,
# переписывает его в полный самостоятельный вопрос ДО того, как он попадёт в classify/search/rerank.
CONTEXTUALIZE_SYSTEM = """
Ты — модуль анализа истории диалога. Твоя задача — понять, ссылается ли текущий вопрос
пользователя на тему предыдущих вопросов, и если да — переписать его в полный самостоятельный вопрос.

Тебе дана история последних вопросов пользователя (от старых к новым) и текущий вопрос.

Правила:
1. Если текущий вопрос уже полный и понятен сам по себе, без истории — верни его БЕЗ ИЗМЕНЕНИЙ
   и "depends_on_context": false.
2. Если текущий вопрос короткий, обрывочный, содержит местоимения ("это", "туда", "он", "их")
   или явно продолжает тему предыдущих вопросов (например "где взять", "а сколько", "почему",
   "а если нет") — перепиши его в полный вопрос, подставив недостающую тему из истории,
   и укажи "depends_on_context": true.
3. Не придумывай фактов, которых не было в истории — только соединяй текущий вопрос с темой
   из истории, не добавляя ничего лишнего.
4. Если история не связана с текущим вопросом по смыслу — верни вопрос без изменений
   и "depends_on_context": false.

Верни ТОЛЬКО JSON: {"standalone_question": "...", "depends_on_context": true/false}. Без пояснений.

Примеры:

История:
- Что нужно для работы на высоте?
- Какие требования к страховочной привязи?
- Какая экипировка нужна для работы на высоте?
Текущий вопрос: "Где взять"
{"standalone_question": "Где взять экипировку для работы на высоте?", "depends_on_context": true}

История:
- Сколько дней отпуска положено?
Текущий вопрос: "А дополнительный?"
{"standalone_question": "Сколько дней дополнительного отпуска положено?", "depends_on_context": true}

История: (пусто или не по теме текущего вопроса)
Текущий вопрос: "Сколько огнетушителей должно быть на складе?"
{"standalone_question": "Сколько огнетушителей должно быть на складе?", "depends_on_context": false}
"""

def resolve_question(question, history):
    """
    history — список последних реплик пользователя вида [{"question": "...", "answer": "..."}, ...]
    от старых к новым (обычно уже обрезан вызывающей стороной до HISTORY_WINDOW).
    """
    if not history:
        log("CONTEXTUALIZE", "История пуста, используем вопрос как есть")
        return {"standalone_question": question, "context_used": False}

    # Явные приветствия/благодарности не нуждаются в переформулировке — не тратим вызов LLM
    if is_greeting_or_general(question):
        log("CONTEXTUALIZE", "Вопрос — приветствие/общая фраза, пропускаем анализ истории")
        return {"standalone_question": question, "context_used": False}

    # Достаточно длинный вопрос с предметными ключевыми словами уже самодостаточен
    if has_rag_keywords(question) and len(question.split()) >= 5:
        log("CONTEXTUALIZE", "Вопрос уже самодостаточен (есть ключевые слова и длина), пропускаем анализ истории")
        return {"standalone_question": question, "context_used": False}

    recent = history[-HISTORY_WINDOW:]
    history_lines = "\n".join(f"- {h['question']}" for h in recent if h.get("question"))
    prompt = f'История последних вопросов пользователя:\n{history_lines}\n\nТекущий вопрос: "{question}"'
    log("CONTEXTUALIZE", f"Анализ вопроса с учётом {len(recent)} предыдущих вопросов")

    raw = small_llm(CONTEXTUALIZE_SYSTEM, prompt, step_name="CONTEXTUALIZE")
    try:
        data = parse_json_response(raw)
        standalone = (data.get("standalone_question") or question).strip() or question
        depends = bool(data.get("depends_on_context", False))
        if depends:
            log("CONTEXTUALIZE", f"Вопрос переформулирован с учётом контекста: '{question}' -> '{standalone}'")
        else:
            log("CONTEXTUALIZE", "Вопрос признан самостоятельным, контекст не использован")
        return {"standalone_question": standalone, "context_used": depends}
    except Exception as e:
        log("CONTEXTUALIZE", f"Ошибка разбора ответа, используем исходный вопрос. Ошибка: {e}")
        return {"standalone_question": question, "context_used": False}

# ---------- Классификация (усиленная) ----------
CLASSIFY_SYSTEM = """
Ты — модуль маршрутизации. Определи, к какому маршруту отнести вопрос пользователя.

Маршруты:
- "rag" — любые вопросы о правилах, регламентах, инструкциях, нормах, процедурах, условиях работы, льготах, отпусках, технике безопасности, охране труда.
  Вопросы часто начинаются с: что, как, где, когда, почему, зачем, сколько, какой, какие, нужно ли, обязан ли, можно ли, разрешено ли.
- "general" — только короткие приветствия, прощания, благодарности без вопросительного смысла. Пример: "привет", "спасибо", "ок".
- "escalate" — если речь о травме, угрозе, насилии, конфликте, плохом самочувствии (требуется вмешательство человека).

Верни ТОЛЬКО JSON с полями: "route" (одно из значений), "risk_flag" (bool, true только для escalate), "risk_type" (строка или null).

Примеры:
{"route": "rag", "risk_flag": false, "risk_type": null}          # для "Сколько дней отпуска?"
{"route": "rag", "risk_flag": false, "risk_type": null}          # для "Что делать в начале смены?"
{"route": "general", "risk_flag": false, "risk_type": null}      # для "Привет"
{"route": "escalate", "risk_flag": true, "risk_type": "конфликт"} # для "Меня ударили"

Не добавляй пояснений, только JSON.
"""

def route_question(question):
    log("CLASSIFY", f"Классификация вопроса: {question}")
    # Быстрый префильтр для приветствий
    if is_greeting_or_general(question):
        log("CLASSIFY", "Быстрый префильтр: general")
        return {"route": "general", "risk_flag": False, "risk_type": None}
    # Если есть ключевые слова RAG — сразу rag, минуя LLM (экономит время)
    if has_rag_keywords(question):
        log("CLASSIFY", "Быстрый префильтр: найдены ключевые слова RAG, маршрут rag")
        return {"route": "rag", "risk_flag": False, "risk_type": None}

    raw = small_llm(CLASSIFY_SYSTEM, question, step_name="CLASSIFY")
    try:
        data = parse_json_response(raw)
        result = {
            "route": data.get("route", "rag"),
            "risk_flag": data.get("risk_flag", False),
            "risk_type": data.get("risk_type"),
        }
        log("CLASSIFY", f"Результат: {result}")
        return result
    except Exception as e:
        log("CLASSIFY", f"Ошибка, возвращаем rag. Ошибка: {e}")
        return {"route": "rag", "risk_flag": False, "risk_type": None}

# ---------- Поиск в Qdrant ----------
def search(question, limit=6, folder_slugs=None):
    """Векторный поиск чанков; при folder_slugs — только внутри этих папок (метка
    payload.folders, массив). Без folder_slugs — по всей общей базе «Все документы»."""
    vector = embed(question)
    payload = {"query": vector, "limit": limit, "with_payload": True}
    if folder_slugs:
        payload["filter"] = {"must": [{"key": "folders", "match": {"any": folder_slugs}}]}
    r = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/query", json=payload, timeout=QDRANT_TIMEOUT)
    r.raise_for_status()
    points = r.json()["result"]["points"]
    log("SEARCH", f"папки {folder_slugs or '(вся база)'}: {len(points)} кандидатов")
    return points


# ---------- Многоуровневый поиск (ТЗ §18–23) ----------
RETRIEVE_MIN = 3   # достаточно кандидатов — не расширяем поиск на следующий уровень
QUESTION_ROUTE_THRESHOLD = 0.35   # ниже — вопрос НЕ считаем отнесённым к папке (тогда общая база)

# ---------- Приоритет по должности сотрудника ----------
# Чанк несёт payload.profession — должность, для которой предназначен документ ("" = общий,
# для всех). Из двух похожих чанков ближе к должности сотрудника должен оказаться тот, чья
# профессия совпала с должностью. Совпадение — по СМЫСЛУ (эмбеддинг), а не по строке: «водитель»
# на чанке ↔ «Водитель автомобиля (автосамосвал)» в профиле. Это приоритет, не жёсткий фильтр:
# общие чанки (profession="") доступны всем без буста; чужая профессия — штраф, а не отсев.
PROF_MATCH_SIM = 0.62      # cos >= — профессия чанка совпала с должностью сотрудника
PROF_MISMATCH_SIM = 0.45   # cos <  — чанк явно для ДРУГОЙ профессии
PROF_BONUS = 0.12
PROF_PENALTY = 0.12

_prof_vec_cache: dict = {}   # текст профессии -> эмбеддинг (профессий немного, кэш живёт в процессе)


def _cos(a, b) -> float:
    if not a or not b:
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


def _prof_vec(text: str):
    v = _prof_vec_cache.get(text)
    if v is None:
        v = embed(text)
        _prof_vec_cache[text] = v
    return v


def _prof_delta_from_sim(sim: float) -> float:
    """Чистое правило приоритета по близости профессии чанка к должности сотрудника."""
    if sim >= PROF_MATCH_SIM:
        return PROF_BONUS
    if sim < PROF_MISMATCH_SIM:
        return -PROF_PENALTY
    return 0.0


def profession_delta(chunk_profession: str, position_vec) -> float:
    """Поправка к скору чанка по совпадению его профессии с должностью сотрудника.
    Пустая профессия (общий документ) или нет должности — 0 (нейтрально)."""
    if not chunk_profession or position_vec is None:
        return 0.0
    try:
        return _prof_delta_from_sim(_cos(position_vec, _prof_vec(chunk_profession)))
    except Exception:
        return 0.0


def _folders_for_stages(stage_ids):
    if not stage_ids:
        return []
    sset = set(stage_ids)
    try:
        return [f["slug"] for f in folders.list_folders(include_disabled=False)
                if sset & set(f.get("stage_ids") or [])]
    except Exception:
        return []


def _merge(dst, src):
    seen = {d["id"] for d in dst}
    for p in src:
        if p["id"] not in seen:
            seen.add(p["id"])
            dst.append(p)
    return dst


def fetch_neighbors(source, chunk_index, span=1):
    """Соседние чанки того же документа (L2, ТЗ §20): проверяем контекст вокруг
    найденного фрагмента — исключения, ограничения, уточнения рядом."""
    if chunk_index is None:
        return []
    payload = {"limit": 2 * span + 2, "with_payload": True, "filter": {"must": [
        {"key": "source", "match": {"value": source}},
        {"key": "chunk_index", "range": {"gte": chunk_index - span, "lte": chunk_index + span}},
    ]}}
    try:
        r = requests.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json=payload, timeout=QDRANT_TIMEOUT)
        r.raise_for_status()
        pts = r.json()["result"]["points"]
        return [(p["payload"].get("raw_text") or p["payload"].get("text", "")) for p in pts]
    except Exception:
        return []


def cascade_search(question, current_stage_ids=None, limit=8, position=None):
    """L1 релевантные папки -> L3 папки текущего этапа -> L4 вся база. Приоритет —
    свежесть документа, текущий этап и должность сотрудника (буст, не жёсткий фильтр —
    ТЗ §6, §24). position — должность сотрудника из профиля (приоритет по профессии чанка)."""
    matched = classify.match_folders(question, top_k=3, threshold=QUESTION_ROUTE_THRESHOLD)
    folder_slugs = [m[0] for m in matched]
    cands = search(question, limit=limit, folder_slugs=folder_slugs or None)

    if len(cands) < RETRIEVE_MIN and current_stage_ids:
        stage_folders = _folders_for_stages(current_stage_ids)
        if stage_folders:
            cands = _merge(cands, search(question, limit=limit, folder_slugs=stage_folders))

    # К общей базе БЕЗ фильтра откатываемся только если вопрос не отнесён ни к одной папке
    # (общий вопрос / тема вне известных папок). Если вопрос отнесён к папке — не подмешиваем
    # чужие темы: лучше вернуть мало точных фрагментов, чем выдать оборудование сварщика
    # пожарному. Недостачу закроет ответ «в базе не нашлось» (маршрут к человеку).
    if len(cands) < RETRIEVE_MIN and not folder_slugs:
        cands = _merge(cands, search(question, limit=limit, folder_slugs=None))

    sset = set(current_stage_ids or [])
    position_vec = None
    if (position or "").strip():
        try:
            position_vec = _prof_vec(position.strip())
        except Exception as e:
            log("SEARCH", f"эмбеддинг должности не получен ({e}) — без приоритета по профессии")
    for p in cands:
        pl = p.get("payload") or {}
        stage_boost = 0.05 if sset & set(pl.get("stage_ids") or []) else 0.0
        prof = profession_delta(pl.get("profession") or "", position_vec)
        p["_prof"] = prof                       # переносится в rerank как приоритет должности
        p["_adj"] = p["score"] + stage_boost + prof
        p["_when"] = pl.get("uploaded_at") or ""
    cands.sort(key=lambda p: (p["_adj"], p["_when"]), reverse=True)
    return cands[:max(limit, 6)]

# ---------- Реранжирование (максимально усиленный промпт) ----------
RERANK_SYSTEM = """
Ты — эксперт по оценке релевантности текстовых фрагментов.

Твоя задача: оценить, насколько данный фрагмент документа соответствует вопросу пользователя.
Оценка должна быть числом от 0.0 до 1.0, где:
- 1.0 — фрагмент полностью и точно отвечает на вопрос, содержит прямую информацию.
- 0.8–0.9 — фрагмент очень релевантен, но не даёт полного ответа.
- 0.5–0.7 — фрагмент частично релевантен, содержит смежную информацию.
- 0.1–0.4 — слабая связь, упоминаются похожие термины, но не по делу.
- 0.0 — совершенно не релевантно, нет никакой связи.

Примеры:
Вопрос: "Сколько дней отпуска положено?"
Фрагмент: "Сотруднику положен отпуск 28 календарных дней в год."
Оценка: 1.0

Вопрос: "Что надеть для работы на высоте?"
Фрагмент: "Работа на высоте разрешена только при наличии страховочного пояса и каски."
Оценка: 1.0

Вопрос: "Что надеть для работы на высоте?"
Фрагмент: "Перед началом смены необходимо пройти инструктаж по технике безопасности."
Оценка: 0.1

Вопрос: "Сколько дней отпуска?"
Фрагмент: "Работа на высоте требует страховки."
Оценка: 0.0

Теперь твоя очередь. Верни ТОЛЬКО JSON с одним полем "relevance", например: {"relevance": 0.95}.
Никаких пояснений, только JSON.
"""

def rerank(question, candidates):
    log("RERANK", f"Реранжирование {len(candidates)} кандидатов для вопроса: {question}")
    scored = []
    for idx, c in enumerate(candidates):
        fragment = c["payload"]["text"]
        prompt = f'Вопрос: "{question}"\nФрагмент: "{fragment}"'
        raw = small_llm(RERANK_SYSTEM, prompt, step_name=f"RERANK_{idx+1}")
        relevance = None
        # Пытаемся извлечь JSON
        try:
            data = parse_json_response(raw)
            if isinstance(data, dict) and "relevance" in data:
                relevance = float(data["relevance"])
        except Exception:
            pass
        # Если JSON не удался, ищем число через regex
        if relevance is None:
            # Берём первое число, похожее на оценку (0..1), а не просто первое в тексте:
            # «1 из 10: 0.3» иначе давало бы 1.0. Только если такого нет — трактуем
            # процент (>1..100) как долю.
            numbers = [float(n) for n in re.findall(r'(\d+\.?\d*)', raw)]
            in_range = next((v for v in numbers if 0.0 <= v <= 1.0), None)
            if in_range is not None:
                relevance = in_range
            else:
                pct = next((v for v in numbers if 1 < v <= 100), None)
                relevance = pct / 100.0 if pct is not None else None
        # Если всё равно None, используем векторный скор как fallback (но только если он > 0.5)
        if relevance is None:
            vector_score = c["score"]
            if vector_score >= 0.5:
                relevance = vector_score * 0.9  # чуть занижаем, чтобы не переоценить
                log("RERANK", f"Fallback: использован векторный скор {vector_score} -> {relevance:.3f}")
            else:
                relevance = 0.0
        # Приоритет по должности сотрудника: из двух одинаково релевантных фрагментов выше
        # окажется тот, чья профессия совпала с должностью; фрагмент для чужой профессии —
        # ниже. Общие фрагменты (profession="") нейтральны (_prof=0).
        relevance = max(0.0, min(1.0, relevance + c.get("_prof", 0.0)))
        scored.append({
            "text": fragment,
            "relevance": relevance,
            "vector_score": c["score"],
        })
        log("RERANK", f"Кандидат {idx+1}: релевантность={relevance:.3f}, векторный скор={c['score']:.3f}, приоритет должности={c.get('_prof', 0.0):+.2f}")
    sorted_scored = sorted(scored, key=lambda x: x["relevance"], reverse=True)
    log("RERANK", f"Результат реранжирования (отсортировано): {[(s['relevance'], s['text'][:40]) for s in sorted_scored]}")
    return sorted_scored

# ---------- Генерация ----------
GENERATE_SYSTEM = """
Ты — ассистент по внутренним регламентам завода.
Отвечай на вопрос сотрудника, используя только предоставленный контекст.
Если в контексте нет информации — скажи, что не знаешь, и предложи обратиться к специалисту.
Отвечай кратко, по делу, на русском языке.
Не рассуждай, не выводи свои мысли — дай только готовый ответ.
"""

def generate_answer(question, context_fragments):
    log("GENERATE", f"Генерация ответа с использованием {len(context_fragments)} фрагментов")
    context_text = "\n\n".join([f"--- Фрагмент {i+1} ---\n{frag}" for i, frag in enumerate(context_fragments)])
    user_prompt = f"Вопрос: {question}\n\nКонтекст:\n{context_text}"
    log("GENERATE", f"Сформирован промпт для большой модели:\n{user_prompt}")
    answer = big_llm(GENERATE_SYSTEM, user_prompt)
    log("GENERATE", f"Сгенерированный ответ: {answer}")
    return answer

# ---------- Основная функция ----------
def handle_question(question, history=None, current_stage_ids=None, position=None):
    """
    history — список предыдущих реплик текущего диалога вида
    [{"question": "...", "answer": "..."}, ...] от старых к новым.
    current_stage_ids — id этапов текущего этапа обучения пользователя (из прогресса);
    используются как приоритет поиска, но не ограничивают его (ТЗ §6).
    position — должность сотрудника из профиля: из двух похожих чанков приоритет получает
    тот, чья профессия ближе к должности (приоритет, не фильтр — общие документы доступны всем).
    """
    log("START", f"Обработка вопроса: {question}")
    total_start = time.time()
    history = (history or [])[-HISTORY_WINDOW:]

    # Разрешаем зависимость от контекста ДО классификации/поиска — короткие вопросы
    # вроде "Где взять" сами по себе не несут смысла для векторного поиска.
    resolved = resolve_question(question, history)
    effective_question = resolved["standalone_question"]
    context_used = resolved["context_used"]

    route_info = route_question(effective_question)
    route = route_info["route"]
    log("HANDLE", f"Маршрут: {route}")

    base_result = {
        "question": question,
        "resolved_question": effective_question if context_used else None,
        "context_used": context_used,
        "classification": route_info,
        "route": route,
    }

    if route == "general":
        return {
            **base_result,
            "candidates": [],
            "top_fragments": [],
            "answer": "Здравствуйте! Чем я могу вам помочь по вопросам регламентов и охраны труда?",
            "elapsed_time": time.time() - total_start,
            "error": None
        }

    if route != "rag":
        return {
            **base_result,
            "candidates": [],
            "top_fragments": [],
            "answer": None,
            "elapsed_time": time.time() - total_start,
            "error": None
        }

    candidates = cascade_search(effective_question, current_stage_ids=current_stage_ids, position=position)
    if not candidates:
        return {
            **base_result,
            "candidates": [],
            "top_fragments": [],
            "answer": None,
            "elapsed_time": time.time() - total_start,
            "error": "Нет кандидатов в Qdrant"
        }

    ranked = rerank(effective_question, candidates)
    top = [r for r in ranked if r["relevance"] >= CONFIDENCE_THRESHOLD]
    top_fragments = [r["text"] for r in top[:MAX_CONTEXT_FRAGMENTS]]

    # L2 — контекст исходных документов: добавляем соседние чанки лучших фрагментов,
    # чтобы не отвечать по вырванному куску без учёта исключений/ограничений рядом.
    context_fragments = list(top_fragments)
    for cand in candidates[:MAX_CONTEXT_FRAGMENTS]:
        pl = cand.get("payload") or {}
        if pl.get("text") in top_fragments:
            for neigh in fetch_neighbors(pl.get("source"), pl.get("chunk_index")):
                if neigh and neigh not in context_fragments:
                    context_fragments.append(neigh)

    answer = None
    if top_fragments:
        answer = generate_answer(effective_question, context_fragments)
    else:
        log("HANDLE", "Confidence gate не пройден")

    return {
        **base_result,
        "candidates": ranked,
        "top_fragments": top_fragments,
        "answer": answer,
        "elapsed_time": time.time() - total_start,
        "error": None
    }

# ---------- Тест ----------
if __name__ == "__main__":
    # Имитация диалога: три вопроса по теме "работа на высоте", затем обрывочный
    # четвёртый вопрос, который должен быть разрешён через историю.
    dialogue = [
        "Что нужно для работы на высоте?",
        "Какие требования к страховочной привязи?",
        "Какая экипировка нужна для работы на высоте?",
        "Где взять",
    ]
    history = []
    for q in dialogue:
        result = handle_question(q, history=history)
        print(f"\n=== Вопрос: {q!r} ===")
        if result.get("context_used"):
            print(f"    Разрешено как: {result['resolved_question']!r}")
        print(f"    Маршрут: {result['route']}")
        print(f"    Ответ: {result.get('answer')}")
        history.append({"question": q, "answer": result.get("answer")})