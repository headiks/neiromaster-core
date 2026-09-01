"""
classify.py — смысловой анализ документов и их частей и отнесение к СУЩЕСТВУЮЩИМ
смысловым папкам. Ключевой принцип ТЗ: ИИ не создаёт папки, только классифицирует
внутрь тех, что завёл человек. Если ничего не подошло — документ всё равно остаётся
в общей базе «Все документы» (вся коллекция чанков), просто без меток папок.

Механизм (ТЗ §19 оставляет выбор реализации за нейросетью — относим по СМЫСЛУ, а
не по словам):
  - для каждой включённой папки строится вектор её «тега» = название + описание +
    критерии (коллекция Qdrant "folder_tags"); критерии — это смысловые признаки,
    а не список ключевых слов;
  - документ/чанк относится к папке, если его вектор достаточно близок к вектору
    папки;
  - на уровне документа выбор среди кандидатов уточняет LLM: она понимает критерии
    тоньше вектора и отсекает ложные совпадения.

Плюс здесь же:
  - summarize_document() — краткое смысловое описание документа (ТЗ §10);
  - find_similar_docs() — поиск похожих/связанных документов при загрузке
    (ТЗ §15–16, дубли и противоречия); краткие описания документов хранятся
    векторами в коллекции "doc_summaries".
"""

import os
import re
import json
from typing import Optional

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue,
)

import folders
from config import QDRANT_HOST, QDRANT_PORT, EMBED_DIM, OLLAMA_URL, get_embedding

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

FOLDER_TAGS = "folder_tags"      # вектор «тега» каждой включённой папки
DOC_SUMMARIES = "doc_summaries"  # вектор краткого описания каждого документа

# Модель для смысловых описаний и уточнения выбора папок. Крупная — операция
# редкая (раз на документ) и требует надёжного следования инструкции.
LLM_MODEL = "qwen3:14b"
LLM_TIMEOUT = 180

DOC_MATCH_THRESHOLD = 0.30    # кандидат-папка для документа (широкий отбор, потом уточняет LLM)
CHUNK_MATCH_THRESHOLD = 0.45  # кандидат-папка для чанка по вектору (дальше подтверждает LLM)
SIMILAR_DOC_THRESHOLD = 0.80  # порог «похожий/связанный документ» при загрузке

# Подтверждать принадлежность чанка папке большой моделью (не только эмбеддингом).
# Точнее разделяет темы, но добавляет по LLM-вызову на чанк с кандидатами. Отключаемо
# на очень большом корпусе: NEIROMASTER_CHUNK_LLM=0.
CHUNK_LLM_CONFIRM = os.environ.get("NEIROMASTER_CHUNK_LLM", "1") not in ("0", "false", "False", "")


def _log(step, msg):
    print(f"[CLASSIFY:{step}] {msg}")


def _ensure(collection):
    names = [c.name for c in client.get_collections().collections]
    if collection not in names:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )


def ensure_collections():
    _ensure(FOLDER_TAGS)
    _ensure(DOC_SUMMARIES)


# ---------- Векторы папок (перестраиваются при любом изменении структуры) ----------
def folder_tag_text(folder: dict) -> str:
    crit = ". ".join(folder.get("criteria") or [])
    return f"{folder['name']}. {folder.get('description', '')}. {crit}".strip()


def sync_folder_vectors() -> int:
    """Полностью пересобирает векторы папок из БД (папок немного — проще целиком).
    Берём только включённые: в выключенные папки новые документы не относим."""
    _ensure(FOLDER_TAGS)
    client.delete_collection(FOLDER_TAGS)
    _ensure(FOLDER_TAGS)
    points = []
    for f in folders.list_folders(include_disabled=False):
        vec = get_embedding(folder_tag_text(f))
        points.append(PointStruct(id=_uuid_for(f["slug"]), vector=vec, payload={
            "slug": f["slug"], "name": f["name"], "stage_ids": f.get("stage_ids") or [],
        }))
    if points:
        client.upsert(collection_name=FOLDER_TAGS, points=points)
    _log("SYNC", f"векторов папок: {len(points)}")
    return len(points)


def _uuid_for(slug: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"folder-{slug}"))


def match_folders_by_vector(vec, top_k: int = 6, threshold: float = DOC_MATCH_THRESHOLD) -> list:
    """[(slug, name, stage_ids, score)] по убыванию близости; только >= threshold."""
    _ensure(FOLDER_TAGS)
    try:
        hits = client.query_points(collection_name=FOLDER_TAGS, query=vec,
                                   limit=top_k, with_payload=True).points
    except Exception as e:
        _log("MATCH", f"ошибка векторного матча: {e}")
        return []
    out = []
    for h in hits:
        if h.score >= threshold:
            p = h.payload
            out.append((p["slug"], p.get("name", p["slug"]), p.get("stage_ids") or [], h.score))
    return out


def match_folders(text: str, top_k: int = 6, threshold: float = DOC_MATCH_THRESHOLD) -> list:
    try:
        vec = get_embedding(text)
    except Exception as e:
        _log("MATCH", f"эмбеддинг не получен: {e}")
        return []
    return match_folders_by_vector(vec, top_k, threshold)


# ---------- Краткое смысловое описание документа (ТЗ §10) ----------
SUMMARY_SYSTEM = """Ты — аналитик базы знаний предприятия. По тексту документа составь
краткое смысловое описание (3–5 предложений, строго по-русски): о чём документ, какие
процессы описывает, на какие вопросы сотрудника может ответить, какие основные темы
затрагивает. Без вступлений и Markdown — только само описание."""


def _llm(system: str, user: str) -> str:
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False, "think": False,
    }, timeout=LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def summarize_document(markdown_text: str, filename: str) -> str:
    try:
        return _llm(SUMMARY_SYSTEM, f"Файл: {filename}\n\n{markdown_text[:6000]}")
    except Exception as e:
        _log("SUMMARY", f"LLM недоступна ({e}) — беру начало текста")
        head = re.sub(r"\s+", " ", markdown_text).strip()
        return head[:600]


# ---------- Профессия/должность документа (для приоритезации по должности сотрудника) ----------
# Универсально, без списков профессий: модель сама называет должность из смысла документа.
# Пусто = документ общий (для всех). Хранится на чанках как payload.profession и при поиске
# используется как приоритет: из двух похожих чанков ближе к должности сотрудника — тот, чья
# профессия совпала. Это НЕ жёсткий фильтр (общие документы доступны всем; ТЗ §6, §24).
PROFESSION_SYSTEM = """Определи, для какой КОНКРЕТНОЙ должности или профессии предназначен
документ. Многие документы общие — относятся ко всем сотрудникам вне зависимости от должности
(положения, кодексы, политики, коллективный договор и т.п.); для них профессии НЕТ.
Документ привязан к профессии, только если его содержание специфично для одной должности
(инструкция по охране труда водителя, программа стажировки сварщика, должностная инструкция
электрика и т.п.).
Верни СТРОГО JSON: {"profession": "<краткое название должности, именительный падеж, ед. число>"}
или {"profession": ""} если документ общий. Без пояснений.
Примеры:
- «Инструкция по охране труда для водителя автомобиля» -> {"profession": "водитель"}
- «Программа стажировки электрогазосварщика» -> {"profession": "электрогазосварщик"}
- «Положение об адаптации персонала» -> {"profession": ""}
- «Коллективный договор» -> {"profession": ""}"""


def detect_profession(summary: str) -> str:
    """Краткое название должности, для которой предназначен документ, или "" для общих.
    Определяется по смысловому описанию большой моделью (универсально, без словарей)."""
    if not (summary or "").strip():
        return ""
    try:
        raw = _llm(PROFESSION_SYSTEM, f"Описание документа:\n{summary[:2000]}")
        return (_parse_json(raw).get("profession") or "").strip()
    except Exception as e:
        _log("PROF", f"профессия не определена ({e})")
        return ""


# Пер-чанковая профессия: документ уже проанализирован целиком (detect_profession дал
# профессию документа); для КАЖДОГО фрагмента решаем, относится ли он к этой профессии или
# он общий (годится всем) — исходя из КОНТЕКСТА всего документа, а не фрагмента в вакууме.
CHUNK_PROF_SYSTEM = """Документ в целом относится к профессии (она указана). Реши, относится ли
ДАННЫЙ ФРАГМЕНТ к этой же профессии или он ОБЩИЙ — то есть годится всем сотрудникам независимо
от должности (общие правила, ценности, оргвопросы, безопасность для всех и т.п.).
Верни СТРОГО JSON: {"profession": "<та же профессия>"} если фрагмент специфичен для неё,
или {"profession": ""} если фрагмент общий. Без пояснений."""


def chunk_profession(chunk_text: str, doc_summary: str, doc_profession: str) -> str:
    """Профессия ОТДЕЛЬНОГО чанка с учётом контекста всего документа. Общий документ
    (doc_profession="") -> чанк общий без вызова LLM (оптимизация). Иначе большая модель
    решает: этот фрагмент специфичен для профессии документа или общий."""
    if not (doc_profession or "").strip() or not (chunk_text or "").strip():
        return ""
    try:
        user = (f"Профессия документа: {doc_profession}\n"
                f"О чём документ: {doc_summary[:1200]}\n\nФрагмент:\n{chunk_text[:1500]}")
        prof = (_parse_json(_llm(CHUNK_PROF_SYSTEM, user)).get("profession") or "").strip()
        # либо профессия документа, либо общий — чужую профессию модель не выдумывает
        return doc_profession if prof else ""
    except Exception as e:
        _log("CHUNK-PROF", f"профессия чанка не определена ({e}) — беру профессию документа")
        return doc_profession


# ---------- Проверка смысловой нагрузки чанка ----------
# В документах много служебного: одиночные заголовки, номера страниц, колонтитулы, строки
# оглавления («Раздел 3 ..... 12»), голые номера пунктов. Такие чанки не должны распределяться
# по этапам/подэтапам/профессии. Проверка эвристическая (дёшево, язык-независимо): по длине,
# доле букв и числу настоящих слов. Полный чанкинг для Q&A это не трогает — только распределение.
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)   # «слово» — 2+ буквы подряд (без цифр)


def is_meaningful(text: str) -> bool:
    """Несёт ли чанк содержательный текст (True) или это служебный мусор — заголовок,
    номер страницы, строка оглавления, номер пункта (False)."""
    t = (text or "").strip()
    if len(t) < 15:
        return False                       # слишком коротко: заголовок/номер
    letters = sum(ch.isalpha() for ch in t)
    if letters < 10 or letters / len(t) < 0.35:
        return False                       # почти одни цифры/пунктуация: номер страницы, «..... 12»
    if len(_WORD_RE.findall(t)) < 3:
        return False                       # меньше трёх настоящих слов — не предложение
    return True


# ---------- Классификация документа (кандидаты по вектору + уточнение LLM) ----------
SELECT_SYSTEM = """Ты относишь документ к смысловым папкам базы знаний. Тебе дают краткое
описание документа и список папок-КАНДИДАТОВ с их критериями. Верни ТОЛЬКО те папки,
к которым документ действительно относится по смыслу критериев. Документ может
относиться сразу к нескольким папкам. Если ни одна не подходит — верни пустой список.
Ответ — СТРОГО JSON: {"folders": ["slug", ...]}. Без пояснений."""


def _parse_json(text: str) -> dict:
    text = re.sub(r"```json\s*|```", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("нет JSON")
    return json.loads(text[a:b + 1])


def classify_document(summary: str) -> dict:
    """Возвращает {"folders": [slug], "stage_ids": [id], "candidates": [{slug,name,score}]}.
    Пустой folders — документ уходит только в общую базу «Все документы»."""
    candidates = match_folders(summary, top_k=6, threshold=DOC_MATCH_THRESHOLD)
    cand_index = {c[0]: c for c in candidates}
    if not candidates:
        return {"folders": [], "stage_ids": [], "candidates": []}

    # Уточняем выбор LLM среди кандидатов (она видит критерии).
    by_slug = {f["slug"]: f for f in folders.list_folders(include_disabled=False)}
    lines = []
    for slug, name, _st, _sc in candidates:
        crit = "; ".join((by_slug.get(slug) or {}).get("criteria") or [])
        lines.append(f"- {slug} ({name}): {crit}")
    chosen = [c[0] for c in candidates]  # запасной вариант, если LLM не ответит
    try:
        raw = _llm(SELECT_SYSTEM, f"Описание документа:\n{summary[:2000]}\n\nПапки-кандидаты:\n" + "\n".join(lines))
        picked = _parse_json(raw).get("folders") or []
        picked = [s for s in picked if s in cand_index]
        if picked:
            chosen = picked
        else:
            # LLM явно сказала «ни одна не подходит» — уважаем: документ уходит ТОЛЬКО в
            # общую базу «Все документы», а не форсится в неподходящие папки по вектору
            # (первую помощь не пихаем в HR-папки, если подходящей папки просто нет).
            chosen = []
    except Exception as e:
        # Только при ОШИБКЕ модели берём векторный запас — иначе доверяем решению LLM.
        _log("SELECT", f"LLM не уточнила выбор ({e}) — беру уверенные векторные как запас")
        chosen = [c[0] for c in candidates if c[3] >= 0.45]

    stage_ids = _stage_union(chosen, by_slug)
    return {
        "folders": chosen,
        "stage_ids": stage_ids,
        "candidates": [{"slug": s, "name": n, "score": round(sc, 3)} for s, n, _st, sc in candidates],
    }


def select_chunk_folders(matches, doc_folders) -> list:
    """Папки чанка = (папки документа) ∩ (папки, к которым близок сам чанк).

    Чистая функция (без Qdrant/БД) — тестируется отдельно. Ключевой инвариант для
    разделения тем: чанк НЕ уходит в папку, к которой не отнесён документ, и НЕ
    наследует автоматически все папки документа. В папке остаются только те чанки,
    которые сами по смыслу ей подходят — иначе фильтр по папке при ответе перестаёт
    отличать темы (оборудование сварщика не должно попадать пожарному).

    matches — [(slug, name, stage_ids, score)] выше порога CHUNK_MATCH_THRESHOLD."""
    doc_set = set(doc_folders or [])
    out = []
    for slug, _name, _st, _score in matches:
        if slug in doc_set and slug not in out:
            out.append(slug)
    return out


CHUNK_SELECT_SYSTEM = """Ты решаешь, к каким смысловым папкам относится ОТДЕЛЬНЫЙ ФРАГМЕНТ
документа. Тебе дают текст фрагмента и папки-КАНДИДАТЫ с их критериями. Верни ТОЛЬКО те
папки, содержанию которых фрагмент действительно соответствует по смыслу критериев — так,
чтобы по вопросу из этой темы фрагмент был уместным ответом. Если фрагмент не подходит ни
одной — верни пустой список. Ответ — СТРОГО JSON: {"folders": ["slug", ...]}. Без пояснений."""


def _llm_confirm_chunk_folders(text: str, cand_slugs: list, by_slug: dict) -> list:
    """Большая модель подтверждает, каким папкам-кандидатам фрагмент реально соответствует
    по критериям (эмбеддинг лишь предложил кандидатов). Пустой список — законный ответ:
    фрагмент не подходит ни одной, остаётся в общей базе. При ошибке модели — возвращаем
    кандидатов как есть (не теряем векторную разметку)."""
    lines = []
    for slug in cand_slugs:
        f = by_slug.get(slug) or {}
        crit = "; ".join(f.get("criteria") or [])
        lines.append(f"- {slug} ({f.get('name', slug)}): {crit}")
    try:
        raw = _llm(CHUNK_SELECT_SYSTEM, f"Фрагмент:\n{text[:1500]}\n\nПапки-кандидаты:\n" + "\n".join(lines))
        picked = _parse_json(raw).get("folders") or []
        return [s for s in picked if s in cand_slugs]
    except Exception as e:
        _log("CHUNK-LLM", f"подтверждение не удалось ({e}) — беру векторных кандидатов")
        return list(cand_slugs)


def classify_chunk(text: str, doc_folders: list, vec=None, confirm: bool = True) -> dict:
    """Метки папок для отдельного чанка. Двухступенчато: эмбеддинг предлагает кандидатов
    (в пределах папок документа), большая модель подтверждает соответствие критериям.
    vec — уже посчитанный вектор чанка (чтобы не эмбеддить повторно при индексации).
    confirm=False — пропустить LLM-подтверждение (векторные кандидаты как есть): нужно для
    переанализа, где иначе на КАЖДЫЙ чанк идёт вызов модели (документ на 78 чанков —
    десятки минут). Чанк без подходящей папки остаётся без меток."""
    matches = (match_folders_by_vector(vec, top_k=5, threshold=CHUNK_MATCH_THRESHOLD)
               if vec is not None else match_folders(text, top_k=5, threshold=CHUNK_MATCH_THRESHOLD))
    cand = select_chunk_folders(matches, doc_folders)
    by_slug = {f["slug"]: f for f in folders.list_folders(include_disabled=False)}
    chunk_folders = _llm_confirm_chunk_folders(text, cand, by_slug) if (cand and confirm and CHUNK_LLM_CONFIRM) else cand
    return {"folders": chunk_folders, "stage_ids": _stage_union(chunk_folders, by_slug)}


def _stage_union(slugs: list, by_slug: dict) -> list:
    out, seen = [], set()
    for s in slugs:
        for sid in (by_slug.get(s) or {}).get("stage_ids") or []:
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


# ---------- Похожие/связанные документы при загрузке (ТЗ §15–16) ----------
def upsert_doc_summary(filename: str, summary: str, uploaded_at: str):
    _ensure(DOC_SUMMARIES)
    import uuid
    vec = get_embedding(summary or filename)
    client.upsert(collection_name=DOC_SUMMARIES, points=[PointStruct(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"doc-{filename}")),
        vector=vec,
        payload={"filename": filename, "summary": summary, "uploaded_at": uploaded_at},
    )])


def delete_doc_summary(filename: str):
    _ensure(DOC_SUMMARIES)
    client.delete(collection_name=DOC_SUMMARIES, points_selector=Filter(
        must=[FieldCondition(key="filename", match=MatchValue(value=filename))]))


def find_similar_docs(summary: str, exclude: str, threshold: float = SIMILAR_DOC_THRESHOLD) -> list:
    """[{filename, summary, score}] — документы с близким смыслом (возможные дубли/
    обновления/противоречия). Решение (удалить старый / оставить оба / уточнение)
    принимает загружающий пользователь."""
    _ensure(DOC_SUMMARIES)
    try:
        vec = get_embedding(summary or exclude)
        hits = client.query_points(collection_name=DOC_SUMMARIES, query=vec,
                                   limit=5, with_payload=True).points
    except Exception:
        return []
    out = []
    for h in hits:
        p = h.payload or {}
        if p.get("filename") and p["filename"] != exclude and h.score >= threshold:
            out.append({"filename": p["filename"], "summary": p.get("summary", ""), "score": round(h.score, 3)})
    return out


if __name__ == "__main__":
    # Чистая логика без сети.
    bs = {"a": {"slug": "a", "stage_ids": ["s1", "s2"]}, "b": {"slug": "b", "stage_ids": ["s2", "s3"]}}
    assert _stage_union(["a", "b"], bs) == ["s1", "s2", "s3"]
    assert _stage_union([], bs) == []
    assert folder_tag_text({"name": "Охрана труда", "description": "d", "criteria": ["c1", "c2"]}) == "Охрана труда. d. c1. c2"
    print("classify: pure logic — OK")
