"""
Автосортировка документов по темам и векторная маршрутизация вопросов к нужным папкам.

Идея:
  - Каждая тема ("Работа на высоте", "Пожарная безопасность", ...) — это одновременно
    физическая папка (data/documents/<slug>/, data/converted/<slug>/) и точка в отдельной
    Qdrant-коллекции "topics" с вектором её тега (название + описание + ключевые слова).

  - Загрузка документа: берём его краткое содержание (заголовок + начало markdown),
    сравниваем вектором с уже существующими темами.
      - Похоже на существующую (score >= TOPIC_MATCH_THRESHOLD) -> кладём туда сразу,
        без вызова LLM (быстрый путь — важно, когда тем станут сотни).
      - Не похоже -> просим модель свериться со списком тем ещё раз (она видит смысл
        тоньше вектора) и либо находит подходящую, либо придумывает новую: название,
        slug, ключевые слова, описание. Создаётся новая папка.

  - Вопрос пользователя: сравниваем вектором с темами, берём top-K подходящих.
    Дальше rag.py ищет чанки ТОЛЬКО среди этих тем (payload-фильтр по полю
    "topic" в основной коллекции) — то есть по факту не по всей базе документов,
    а только по нужным папкам. Не найдено уверенных совпадений -> ищем по всем темам
    (лучше найти что-то не в той папке, чем ничего не найти).
"""

import re
import json
import uuid
import threading
from typing import Optional

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue

from config import DOCS_DIR, CONVERTED_DIR, QDRANT_HOST, QDRANT_PORT, EMBED_DIM, OLLAMA_URL, get_embedding

TOPICS_COLLECTION = "topics"
# Классификация документа по теме — редкая операция (раз на загрузку), поэтому берём
# крупную модель: она надёжно держит инструкцию «название строго по-русски» и не
# плодит почти одинаковые темы. Малая qwen2.5:3b давала названия вперемешку с
# латиницей/транслитом.
TOPIC_MODEL = "qwen3:14b"
TOPIC_LLM_TIMEOUT = 120

# Порог уверенного векторного матча: чуть ниже прежнего (0.80) — документы охотнее
# приклеиваются к уже существующей теме, папок-одиночек становится меньше.
TOPIC_MATCH_THRESHOLD = 0.72    # уверенный векторный матч с темой -> кладём без вызова LLM
TOPIC_ROUTE_THRESHOLD = 0.35    # порог для маршрутизации вопроса; ниже -> ищем по всем темам сразу
TOPIC_ROUTE_TOP_K = 2           # сколько тем максимум подключаем к одному вопросу

GENERAL_SLUG = "obshee"
GENERAL_NAME = "Общие вопросы"
GENERAL_DESCRIPTION = "Документы, которые не удалось уверенно отнести к более узкой теме"

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
_lock = threading.Lock()


def _log(step, msg):
    print(f"[TOPICS:{step}] {msg}")


def ensure_topics_collection():
    names = [c.name for c in client.get_collections().collections]
    if TOPICS_COLLECTION not in names:
        client.create_collection(
            collection_name=TOPICS_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
        _log("INIT", f"Коллекция '{TOPICS_COLLECTION}' создана")


def slugify(text: str) -> str:
    """Человекочитаемое латинское имя папки из русского названия темы."""
    text = text.lower().strip()
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    })
    text = text.translate(table)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or f"topic_{uuid.uuid4().hex[:6]}"


def _tag_text(name: str, description: str, keywords: list) -> str:
    kw = ", ".join(keywords or [])
    return f"{name}. {description}. Ключевые слова: {kw}".strip()


def list_topics() -> list:
    ensure_topics_collection()
    points, _ = client.scroll(collection_name=TOPICS_COLLECTION, limit=500, with_payload=True, with_vectors=False)
    return [p.payload for p in points]


def get_topic(slug: str) -> Optional[dict]:
    ensure_topics_collection()
    points, _ = client.scroll(
        collection_name=TOPICS_COLLECTION,
        scroll_filter=Filter(must=[FieldCondition(key="slug", match=MatchValue(value=slug))]),
        limit=1, with_payload=True, with_vectors=False,
    )
    return points[0].payload if points else None


def _upsert_topic_point(slug, name, description, keywords, doc_count):
    vec = get_embedding(_tag_text(name, description, keywords))
    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"topic-{slug}"))
    client.upsert(collection_name=TOPICS_COLLECTION, points=[PointStruct(
        id=point_id,
        vector=vec,
        payload={
            "slug": slug,
            "name": name,
            "description": description,
            "keywords": keywords,
            "doc_count": doc_count,
            "folder": str(DOCS_DIR / slug),
        },
    )])


def create_topic(slug: str, name: str, description: str = "", keywords: Optional[list] = None) -> dict:
    ensure_topics_collection()
    with _lock:
        existing = get_topic(slug)
        if existing:
            return existing
        keywords = keywords or []
        (DOCS_DIR / slug).mkdir(parents=True, exist_ok=True)
        (CONVERTED_DIR / slug).mkdir(parents=True, exist_ok=True)
        _upsert_topic_point(slug, name, description, keywords, doc_count=0)
        _log("CREATE", f"Новая тема «{name}» ({slug})")
        return get_topic(slug)


def bump_doc_count(slug: str, delta: int = 1) -> int:
    """Меняет счётчик документов темы и возвращает новое значение (0, если темы нет)."""
    topic = get_topic(slug)
    if not topic:
        return 0
    new_count = max(0, topic.get("doc_count", 0) + delta)
    _upsert_topic_point(slug, topic["name"], topic.get("description", ""), topic.get("keywords", []), new_count)
    return new_count


def delete_topic(slug: str) -> bool:
    """
    Удаляет тему целиком: точку в Qdrant + физические папки data/documents/<slug>
    и data/converted/<slug>. Папку сносим только если она пуста — страховка от
    удаления вместе с чьими-то оставшимися файлами. Вызывается, когда из папки
    убран последний документ (doc_count дошёл до 0). Тема при необходимости
    заведётся заново при следующей загрузке документа.
    """
    ensure_topics_collection()
    with _lock:
        if not get_topic(slug):
            return False
        client.delete(
            collection_name=TOPICS_COLLECTION,
            points_selector=Filter(must=[FieldCondition(key="slug", match=MatchValue(value=slug))]),
        )
        for base in (DOCS_DIR, CONVERTED_DIR):
            folder = base / slug
            if folder.is_dir():
                try:
                    folder.rmdir()          # снимется только если папка пуста
                except OSError:
                    _log("DELETE", f"Папка {folder} не пуста — оставляю")
        _log("DELETE", f"Тема удалена (не осталось документов): {slug}")
        return True


def register_manual_topic(slug: str) -> dict:
    """Документ уже лежал в data/documents/<slug>/ при запуске индексации (ручная
    раскладка оператором) — регистрируем тему по имени папки, если её ещё нет."""
    existing = get_topic(slug)
    if existing:
        return existing
    name = slug.replace("_", " ").strip().capitalize() or slug
    return create_topic(slug, name, description="Тема создана по имени папки (документ размещён вручную)")


# ---------- Классификация документа при загрузке ----------
CLASSIFY_DOC_SYSTEM = """
Ты — модуль автосортировки документов по смысловым папкам (темам) базы знаний предприятия.
На входе: список уже существующих тем (slug и русское название) и краткое содержание нового документа.

Правила:
1. СНАЧАЛА пытайся отнести документ к одной из существующих тем. Выбирай существующую,
   если документ в целом о том же (та же область: охрана труда, безопасность, процедуры,
   аудиты, адаптация и т.п.). Верни её slug и "is_new": false. Не создавай почти
   одинаковые темы-дубли.
2. Новую тему создавай ТОЛЬКО если документ явно не вписывается ни в одну существующую.
3. Тема должна быть УМЕРЕННО ШИРОКОЙ — охватывать класс документов, а не один файл.
   Примеры хороших тем: «Охрана труда», «Пожарная безопасность», «Работа на высоте»,
   «Аудиты и проверки», «Готовность к чрезвычайным ситуациям», «Управление документами»,
   «Адаптация персонала», «Показатели и отчётность».
4. Поле "name" — СТРОГО на русском языке, кириллицей, 2–4 слова, осмысленное и
   человекочитаемое. ЗАПРЕЩЕНО: латиница, английские слова, аббревиатуры латиницей,
   имя файла, транслит. Если в документе английский термин — переведи его на русский
   (FMCSA audit -> «Аудиты и проверки», Emergency Preparedness -> «Готовность к ЧС»).
5. Поле "slug" — короткий транслит латиницей в snake_case (2–4 слова), это имя папки.
6. Поле "description" — одно короткое предложение по-русски, о чём тема.

Верни ТОЛЬКО JSON:
{"slug": "...", "name": "...", "description": "...", "keywords": ["...", "..."], "is_new": true/false}
Без пояснений. Значение "name" — только кириллица.
"""


def _llm_call(system: str, user: str) -> str:
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": TOPIC_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": False,        # qwen3: без «размышлений» — сразу JSON, быстрее
    }, timeout=TOPIC_LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()["message"]["content"]


_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def is_russian_name(name: str) -> bool:
    """Название темы осмысленно и по-русски: есть кириллица, нет латиницы."""
    if not name or not name.strip():
        return False
    return bool(_CYRILLIC_RE.search(name)) and not _LATIN_RE.search(name)


def _parse_json(text: str) -> dict:
    text = re.sub(r"```json\s*|```", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("В ответе нет JSON")
    return json.loads(text[start:end + 1])


def classify_document(doc_gist: str) -> dict:
    """
    Определяет тему (папку) для нового документа, создаёт её при необходимости.
    Возвращает {"slug": "...", "name": "...", "is_new": bool}.
    """
    ensure_topics_collection()
    existing = list_topics()

    # Быстрый путь: вектор документа уверенно близок к уже существующей теме —
    # не тратим вызов LLM (важно при росте базы до сотен документов).
    if existing:
        vec = get_embedding(doc_gist)
        # ИСПРАВЛЕНО: используем query_points вместо search
        hits = client.query_points(
            collection_name=TOPICS_COLLECTION,
            query=vec,
            limit=1,
            with_payload=True
        ).points
        if hits and hits[0].score >= TOPIC_MATCH_THRESHOLD:
            payload = hits[0].payload
            _log("CLASSIFY", f"Уверенный векторный матч: «{payload['name']}» (score={hits[0].score:.3f})")
            bump_doc_count(payload["slug"], +1)
            return {"slug": payload["slug"], "name": payload["name"], "is_new": False}

    # Иначе — сверяемся с моделью: она видит смысл тоньше, чем сырой вектор
    topics_list = "\n".join(f"- {t['slug']}: {t['name']}" for t in existing) or "(тем пока нет)"
    prompt = f"Существующие темы:\n{topics_list}\n\nНовый документ:\n{doc_gist[:1500]}"
    try:
        raw = _llm_call(CLASSIFY_DOC_SYSTEM, prompt)
        data = _parse_json(raw)
        slug = (data.get("slug") or "").strip()
        is_new = bool(data.get("is_new", True))

        if not is_new and slug:
            topic = get_topic(slug)
            if topic:
                bump_doc_count(slug, +1)
                return {"slug": slug, "name": topic["name"], "is_new": False}
            # модель сослалась на несуществующую тему — считаем как новую ниже

        name = (data.get("name") or "").strip()
        # Латинское/пустое название — модель не выполнила требование «строго по-русски».
        # Не заводим папку с нечитаемым именем: кладём в «Общие вопросы».
        if not is_russian_name(name):
            _log("CLASSIFY", f"Название «{name}» не по-русски — в «{GENERAL_NAME}»")
            create_topic(GENERAL_SLUG, GENERAL_NAME, GENERAL_DESCRIPTION)
            bump_doc_count(GENERAL_SLUG, +1)
            return {"slug": GENERAL_SLUG, "name": GENERAL_NAME, "is_new": False}
        slug = slugify(slug or name)
        create_topic(slug, name, data.get("description", ""), data.get("keywords", []))
        bump_doc_count(slug, +1)
        _log("CLASSIFY", f"Создана/выбрана тема «{name}» ({slug})")
        return {"slug": slug, "name": name, "is_new": True}

    except Exception as e:
        _log("CLASSIFY", f"Не удалось классифицировать, кладём в «{GENERAL_NAME}». Ошибка: {e}")
        create_topic(GENERAL_SLUG, GENERAL_NAME, GENERAL_DESCRIPTION)
        bump_doc_count(GENERAL_SLUG, +1)
        return {"slug": GENERAL_SLUG, "name": GENERAL_NAME, "is_new": False}


# ---------- Маршрутизация вопроса к темам (запросная сторона) ----------
def route_question(question: str, top_k: int = TOPIC_ROUTE_TOP_K, threshold: float = TOPIC_ROUTE_THRESHOLD) -> list:
    """
    Возвращает список slug тем, релевантных вопросу. Именно по ним потом фильтруется
    поиск чанков — не по всей базе, а только по нужным папкам. Пустой список означает,
    что уверенных совпадений нет — поиск идёт по всем темам без фильтра (graceful fallback).
    """
    ensure_topics_collection()
    topics = list_topics()
    if not topics:
        return []

    vec = get_embedding(question)
    # ИСПРАВЛЕНО: используем query_points вместо search
    hits = client.query_points(
        collection_name=TOPICS_COLLECTION,
        query=vec,
        limit=top_k,
        with_payload=True
    ).points
    matched = [h.payload["slug"] for h in hits if h.score >= threshold]
    _log("ROUTE", f"Вопрос -> темы: {matched or '(без фильтра, искать по всем)'} "
                  f"(скоры: {[round(h.score, 3) for h in hits]})")
    return matched


if __name__ == "__main__":
    # Самопроверка гейта русских названий тем (без сети/Qdrant).
    assert is_russian_name("Охрана труда")
    assert is_russian_name("Готовность к ЧС")
    assert not is_russian_name("Ohrana truda")          # транслит
    assert not is_russian_name("LBC Эmergency")         # смешанное — есть латиница
    assert not is_russian_name("FMCSA")                 # аббревиатура латиницей
    assert not is_russian_name("")                      # пусто
    print("topics: гейт русских названий — OK")