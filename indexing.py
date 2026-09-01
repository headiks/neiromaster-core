"""
Индексация документов по модели «человек управляет структурой, ИИ классифицирует
внутрь неё» (см. ТЗ). Используется CLI (index_documents.py) и веб-приложением (app.py).

Путь документа от загрузки до готовности к нейропоиску:

    оригинал (pdf/docx/...), хранится ОДИН раз плоско в data/documents/
        -> DocumentConverter (docling): разбор макета, таблиц, заголовков
           (DoclingDocument кэшируется как JSON в data/processed)
        -> classify.summarize_document(): краткое смысловое описание документа
        -> classify.find_similar_docs(): поиск похожих (дубли/противоречия)
        -> classify.classify_document(): отнесение к СУЩЕСТВУЮЩИМ папкам (по смыслу)
        -> HybridChunker + перекрытие: смысловые чанки с сохранением контекста границ
        -> classify.classify_chunk(): мульти-лейбл метки папок и этапов у каждого чанка
        -> эмбеддинг (bge-m3) -> Qdrant, payload: folders[], stage_ids[], summary, uploaded_at

Папка — логическая метка, а не физическая директория: документ и его чанки могут
относиться к нескольким папкам сразу, а все документы всегда попадают в общую базу
«Все документы» (вся коллекция) независимо от папок.
"""

import re
import json
import math
import time
import uuid
import queue
import hashlib
import threading
from pathlib import Path
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue, MatchAny,
    PayloadSchemaType,
)

from docling.document_converter import DocumentConverter
from docling.chunking import HybridChunker
from docling_core.types.doc.document import DoclingDocument

import classify
import folders
import documents
from config import (
    DOCS_DIR, CONVERTED_DIR, CACHE_DIR, REGISTRY_PATH,
    SUPPORTED_EXT, MAX_UPLOAD_BYTES,
    QDRANT_HOST, QDRANT_PORT, EMBED_DIM, get_embedding, FileGuard,
)

# Символическое перекрытие между соседними чанками (ТЗ §12): в текст для эмбеддинга
# следующего чанка добавляется «хвост» предыдущего, чтобы информация на границе не
# терялась. Доля от чанка, а не жёсткое число — грубая, но рабочая эвристика.
# ponytail: фиксированная доля; при желании стратегию можно усложнить под модель/структуру.
OVERLAP_CHARS = 240

# ---------- Qdrant (основная коллекция чанков) ----------
COLLECTION_NAME = "reglaments"

MAX_TOKENS = 512     # бюджет токенов на чанк (под окно эмбеддинг-модели)
MERGE_PEERS = True   # склеивать соседние мелкие чанки одного уровня иерархии
UPSERT_BATCH = 64

client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
converter = DocumentConverter()
chunker = HybridChunker(max_tokens=MAX_TOKENS, merge_peers=MERGE_PEERS)

# Межпроцессная блокировка реестра: read-modify-write registry.json безопасен и при
# нескольких uvicorn-воркерах (см. config.FileGuard). Раньше был обычный threading.Lock,
# который между процессами не действует — параллельные записи теряли обновления.
_registry_lock = FileGuard(REGISTRY_PATH.with_suffix(".lock"))


def _log(step, msg):
    print(f"[INDEX:{step}] {msg}")


# ---------- Реестр документов (статус, тема, кол-во чанков, ошибки) ----------
def _load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_registry(reg: dict):
    tmp_path = REGISTRY_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    tmp_path.replace(REGISTRY_PATH)


def _update_registry(filename: str, **fields):
    with _registry_lock:
        reg = _load_registry()
        entry = reg.get(filename, {"filename": filename})
        entry.update(fields)
        reg[filename] = entry
        _save_registry(reg)
        return entry


def list_documents() -> list:
    with _registry_lock:
        reg = _load_registry()
    return sorted(reg.values(), key=lambda e: e.get("uploaded_at", ""), reverse=True)


def set_clarification(filename: str, text: str) -> bool:
    """Сохраняет текстовое уточнение пользователя к документу (ТЗ §17). Исходный
    документ не меняется — уточнение хранится в реестре как доп. контекст."""
    with _registry_lock:
        reg = _load_registry()
        if filename not in reg:
            return False
        reg[filename]["clarification"] = text
        _save_registry(reg)
    return True


def folder_doc_counts() -> dict:
    """slug папки -> сколько документов к ней отнесено (по реестру). Папка —
    логическая метка, документ может входить сразу в несколько папок."""
    with _registry_lock:
        registry = _load_registry()
    counts: dict[str, int] = {}
    for doc in registry.values():
        for slug in doc.get("folders") or []:
            counts[slug] = counts.get(slug, 0) + 1
    return counts


# ---------- Вспомогательные функции ----------
def safe_filename(filename: str) -> str:
    """Убираем путь и опасные символы — защита от path traversal при загрузке."""
    name = Path(filename).name
    name = re.sub(r"[^\w\-. а-яА-ЯёЁ]", "_", name)
    return name or f"document_{uuid.uuid4().hex[:8]}"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


# Поля payload, по которым идёт фильтрация при поиске. Без явного индекса Qdrant
# фильтрует полным перебором точек — на 100-200 документах (тысячи чанков) это
# заметно медленнее. Индекс делает отбор по теме/источнику почти бесплатным,
# поэтому двухступенчатый поиск (сначала темы, потом чанки внутри них) масштабируется.
PAYLOAD_INDEXES = {
    "folders": PayloadSchemaType.KEYWORD,        # массив slug'ов — MatchAny фильтрует по папкам
    "stage_ids": PayloadSchemaType.KEYWORD,      # массив id блоков знаний — приоритет по текущему этапу
    "plan_stages": PayloadSchemaType.KEYWORD,    # id этапов каталога — «папки этапов» для генерации плана
    "plan_substages": PayloadSchemaType.KEYWORD, # id подэтапов каталога — «папки подэтапов» (тоньше этапа)
    "meaningful": PayloadSchemaType.BOOL,        # содержательный чанк (мусор в распределение не идёт)
    "source": PayloadSchemaType.KEYWORD,
    "section": PayloadSchemaType.KEYWORD,
}


def ensure_payload_indexes():
    for field, schema in PAYLOAD_INDEXES.items():
        try:
            client.create_payload_index(COLLECTION_NAME, field_name=field, field_schema=schema)
        except Exception as exc:
            # Индекс уже есть — Qdrant отвечает ошибкой, это не фатально.
            _log("INDEX", f"payload-индекс {field}: {exc}")


def create_collection(recreate: bool = False):
    collections = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME in collections:
        if recreate:
            client.delete_collection(COLLECTION_NAME)
        else:
            ensure_payload_indexes()
            classify.ensure_collections()
            return
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )
    ensure_payload_indexes()
    classify.ensure_collections()


def extract_page_no(chunk) -> Optional[int]:
    try:
        return chunk.meta.doc_items[0].prov[0].page_no
    except (AttributeError, IndexError, TypeError):
        return None


# ---------- Конвертация с кэшированием ----------
def convert_document(filepath: Path) -> DoclingDocument:
    """
    Гоняем файл через docling. Разбор PDF/DOCX (особенно PDF с layout-моделью)
    — самая тяжёлая часть пайплайна, поэтому результат кэшируется в JSON:
    при повторной обработке того же файла (переезд между папками, смена
    параметров чанкинга) можно переиспользовать готовую структуру документа.
    """
    cache_path = CACHE_DIR / f"{filepath.stem}__{file_hash(filepath)}.json"
    if cache_path.exists():
        return DoclingDocument.load_from_json(str(cache_path))

    result = converter.convert(str(filepath))
    doc = result.document
    doc.save_as_json(str(cache_path))
    return doc


# Номер пункта регламента в начале строки: «5», «5.1», «5.1.2», с точкой или скобкой.
# Регламенты часто нумеруют пункты цифрами без стилей заголовков — docling такой
# документ отдаёт сплошным текстом, и без этого деления один пункт не отделить от другого.
CLAUSE_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+){0,3})[.)]?[ \t]+(?=\S)")


def sectionize_by_clause(text: str) -> list:
    """
    Делит текст чанка на пункты по номерам в начале строки («5.1 ...»).
    Возвращает [(section_label, segment_text), ...]. Меньше двух номеров —
    возвращаем чанк целиком (section = единственный номер или None).
    Применяется только когда у чанка нет заголовков от docling (см. index_document).
    """
    marks = [(m.start(), m.group(1)) for m in CLAUSE_RE.finditer(text)]
    if len(marks) < 2:
        label = marks[0][1] if marks else None
        return [(label, text.strip())]
    segments = []
    for i, (pos, num) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        seg = text[pos:end].strip()
        if seg:
            segments.append((num, seg))
    return segments


# ---------- Индексация одного документа ----------
def index_document(filepath: Path) -> dict:
    """
    Приём документа по новой модели (ТЗ §9–14):
      сохранённый оригинал (плоско в data/documents/) -> docling
      -> краткое смысловое описание (classify.summarize_document)
      -> проверка похожих документов (дубли/противоречия)
      -> смысловой чанкинг с перекрытием
      -> мульти-лейбл классификация чанков в СУЩЕСТВУЮЩИЕ папки (по вектору)
      -> Qdrant (payload: folders[], stage_ids[], summary, uploaded_at)
    Файл физически один; папка — логическая метка. Все документы попадают в общую
    базу «Все документы» (вся коллекция) независимо от того, отнеслись ли они к папкам.
    """
    filename = filepath.name
    _update_registry(filename, status="processing", error=None)

    try:
        start = time.time()
        doc = convert_document(filepath)
        markdown_text = doc.export_to_markdown()

        # ---- Краткое смысловое описание документа ----
        summary = classify.summarize_document(markdown_text, filename)
        uploaded_at = _load_registry().get(filename, {}).get("uploaded_at") or time.strftime("%Y-%m-%dT%H:%M:%S")

        # ---- Похожие/связанные документы (дубли, обновления, противоречия) ----
        similar = classify.find_similar_docs(summary, exclude=filename)

        # ---- Классификация документа в существующие папки ----
        doc_cls = classify.classify_document(summary)
        doc_folders = doc_cls["folders"]

        # ---- Профессия документа (приоритет поиска по должности сотрудника) ----
        doc_profession = classify.detect_profession(summary)

        # ---- Markdown-версия (плоско) ----
        md_path = CONVERTED_DIR / f"{filepath.stem}.md"
        md_path.write_text(markdown_text, encoding="utf-8")

        _update_registry(
            filename,
            summary=summary,
            folders=doc_folders,
            stage_ids=doc_cls["stage_ids"],
            profession=doc_profession,
            similar=similar,
            path=filename,
            markdown_path=md_path.name,
        )

        # ---- Чанкинг ----
        chunks = list(chunker.chunk(doc))
        if not chunks:
            _update_registry(filename, status="error", error="Не удалось выделить ни одного чанка")
            return {"filename": filename, "status": "error", "error": "Нет чанков"}

        delete_document_vectors(filename)  # переиндексация: сносим прежние чанки

        src_hash = file_hash(filepath)
        points = []
        seg_index = 0
        prev_tail = ""   # хвост предыдущего чанка для перекрытия контекста (ТЗ §12)
        for chunk in chunks:
            headings = list(getattr(chunk.meta, "headings", None) or [])
            page_no = extract_page_no(chunk)

            if headings:
                section = " / ".join(h for h in headings if h)
                segments = [(section, chunk.text, chunker.contextualize(chunk=chunk))]
            else:
                segments = [
                    (label, seg, f"[{label}] {seg}" if label else seg)
                    for label, seg in sectionize_by_clause(chunk.text)
                ]

            for section, raw_text, base_text in segments:
                if not (raw_text or "").strip():
                    continue
                # Перекрытие: добавляем хвост предыдущего сегмента в текст для эмбеддинга.
                text_for_embedding = (f"…{prev_tail}\n\n{base_text}" if prev_tail else base_text)
                prev_tail = raw_text[-OVERLAP_CHARS:]

                vec = get_embedding(text_for_embedding)
                # Смысловая нагрузка: служебный мусор (заголовок, номер страницы, оглавление)
                # не распределяем по папкам/профессии — только содержательные чанки.
                meaningful = classify.is_meaningful(base_text)
                chunk_cls = classify.classify_chunk(base_text, doc_folders, vec=vec) if meaningful \
                    else {"folders": [], "stage_ids": []}
                # Профессия — ПЕР-ЧАНК с учётом контекста всего документа (не одна метка на документ).
                chunk_prof = classify.chunk_profession(base_text, summary, doc_profession) if meaningful else ""
                # Этапы/подэтапы каталога — сразу при индексации: чанк готов для персональных
                # планов без отдельной ручной раскладки. Мусор (meaningful=False) не тегируем.
                plan_stages, plan_substages = _stage_tags(vec) if meaningful else ([], [])
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{filename}-{src_hash}-{seg_index}"))
                points.append(PointStruct(
                    id=point_id,
                    vector=vec,
                    payload={
                        "text": text_for_embedding,
                        "raw_text": raw_text,
                        "source": filename,
                        "meaningful": meaningful,
                        "folders": chunk_cls["folders"],
                        "stage_ids": chunk_cls["stage_ids"],
                        "plan_stages": plan_stages,
                        "plan_substages": plan_substages,
                        "profession": chunk_prof,
                        "section": section,
                        "headings": headings,
                        "page": page_no,
                        "chunk_index": seg_index,
                        "length": len(text_for_embedding),
                        "doc_summary": summary,
                        "uploaded_at": uploaded_at,
                    }
                ))
                seg_index += 1

        if not points:
            _update_registry(filename, status="error", error="После сегментации не осталось текста")
            return {"filename": filename, "status": "error", "error": "Нет чанков"}

        for start_i in range(0, len(points), UPSERT_BATCH):
            client.upsert(collection_name=COLLECTION_NAME, points=points[start_i:start_i + UPSERT_BATCH])

        # Вектор краткого описания — для будущего поиска похожих документов.
        classify.upsert_doc_summary(filename, summary, uploaded_at)

        elapsed = time.time() - start
        _update_registry(
            filename, status="indexed", chunks=len(points), error=None,
            indexed_in_seconds=round(elapsed, 2), folders=doc_folders,
            stage_ids=doc_cls["stage_ids"], profession=doc_profession,
            path=filename, markdown_path=md_path.name,
        )
        # Единый реестр метаданных в PostgreSQL (дедуп по хэшу + экран «этапы↔документы»).
        # Сбой реестра не должен ронять индексацию — документ уже в Qdrant и в registry.json.
        try:
            documents.record(
                sha256=src_hash, filename=filename, summary=summary,
                folders=doc_folders, stage_ids=doc_cls["stage_ids"],
                size_bytes=filepath.stat().st_size, mime=filepath.suffix.lstrip(".").lower(),
                uploaded_at=uploaded_at,
            )
        except Exception as e:
            _log("DOCMETA", f"не удалось записать метаданные {filename} в БД: {e}")

        _log("DONE", f"{filename}: {len(points)} чанков за {elapsed:.2f} сек, папки: {doc_folders or '(общая база)'}")
        return {"filename": filename, "status": "indexed", "chunks": len(points),
                "elapsed": elapsed, "folders": doc_folders, "similar": similar}

    except Exception as e:
        _update_registry(filename, status="error", error=str(e))
        return {"filename": filename, "status": "error", "error": str(e)}


def _query_chunks(vector, query_filter, limit: int):
    """Векторный поиск чанков в Qdrant с (опциональным) фильтром payload. Возвращает
    список точек (у каждой .score и .payload). query_filter=None — без фильтра."""
    return client.query_points(
        collection_name=COLLECTION_NAME, query=vector, limit=limit,
        with_payload=True, query_filter=query_filter,
    ).points


def search_chunks(query_text: str, topic_slugs: Optional[list] = None, limit: int = 6,
                  plan_stage: Optional[str] = None, plan_substage: Optional[str] = None,
                  position: str = "") -> list:
    """
    Поиск чанков с сужением до тем (папок) и до ЭТАПА каталога адаптации (plan_stage —
    «папка этапа»: чанки, разложенные по этому этапу). Из двух похожих чанков приоритет
    получает тот, чья профессия ближе к должности плана (position) — механика приоритета
    по должности из rag. Если по этапу ничего нет (чанки ещё не разложены) — фолбэк без него.
    Возвращает [{"text", "source", "folders", "section", "page", "score"}].
    """
    vector = get_embedding(query_text)
    # Только содержательные чанки (мусор — заголовки/номера страниц — в генерацию не идёт).
    # meaningful != False ловит и старые чанки без поля (там его просто нет).
    base = [FieldCondition(key="meaningful", match=MatchValue(value=True))]
    if topic_slugs:
        base.append(FieldCondition(key="folders", match=MatchAny(any=list(topic_slugs))))

    fetch = max(limit * 3, limit) if position else limit
    # Сужение сверху вниз: подэтап -> этап -> без сужения. Первый непустой уровень выигрывает,
    # чтобы генерация работала и до полной раскладки «папок подэтапов».
    scope_filters = []
    if plan_substage:
        scope_filters.append(FieldCondition(key="plan_substages", match=MatchValue(value=plan_substage)))
    if plan_stage:
        scope_filters.append(FieldCondition(key="plan_stages", match=MatchValue(value=plan_stage)))
    hits = []
    for scope in scope_filters:
        hits = _query_chunks(vector, Filter(must=base + [scope]), fetch)
        if hits:
            break
    if not hits:
        hits = _query_chunks(vector, Filter(must=base) if base else None, fetch)
    # Совсем пусто (старые чанки без meaningful) — ищем без базового фильтра.
    if not hits:
        hits = _query_chunks(vector, None, fetch)

    if position:
        from rag import profession_delta, _prof_vec
        try:
            pv = _prof_vec(position)
        except Exception:
            pv = None
        hits = sorted(hits, key=lambda h: h.score + profession_delta((h.payload or {}).get("profession") or "", pv),
                      reverse=True)[:limit]

    return [{
        "text": h.payload.get("text", ""),
        "source": h.payload.get("source"),
        "folders": h.payload.get("folders") or [],
        "section": h.payload.get("section"),
        "page": h.payload.get("page"),
        "score": h.score,
    } for h in hits]


# ---------- Материализация «папок этапов»: раскладка чанков по этапам каталога ----------
PLAN_STAGE_MATCH = 0.45   # нижняя граница ПОКАЗА кандидата в обосновании (не привязки)
# bge-m3 на русском даёт высокий «пол» косинуса (0.45–0.55 у любых двух текстов),
# поэтому по абсолютному порогу документ цепляется к десяткам подэтапов. Привязку делаем
# ОТНОСИТЕЛЬНОЙ: держим только подэтапы, близкие к лучшему для этого чанка, и не ниже пола.
PLAN_SUBSTAGE_FLOOR = 0.55   # ниже — точно шум (канцелярит/метаданные), не привязываем
PLAN_SUBSTAGE_MARGIN = 0.04  # отставание от лучшего для чанка, дальше — обрыв
PLAN_SUBSTAGE_TOPK = 2       # максимум подэтапов на чанк


def _select_substages(scored):
    """Из [(stage_id, sub_id, score)] оставляет только уверенные привязки чанка:
    топ по score, в пределах MARGIN от лучшего и не ниже FLOOR, максимум TOPK.
    Так один чанк ложится в 0–3 подэтапа, а не в половину каталога."""
    scored = sorted(scored, key=lambda x: x[2], reverse=True)
    if not scored or scored[0][2] < PLAN_SUBSTAGE_FLOOR:
        return []
    best = scored[0][2]
    out = []
    for stid, sub_id, sc in scored:
        if sc < PLAN_SUBSTAGE_FLOOR or sc < best - PLAN_SUBSTAGE_MARGIN:
            break
        out.append((stid, sub_id, sc))
        if len(out) >= PLAN_SUBSTAGE_TOPK:
            break
    return out


def _cos(a, b) -> float:
    if not a or not b:
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


_CATALOG_STAGE_VECS = None   # ((stage_id, vec)...), ((stage_id, sub_id, vec)...)


def _catalog_stage_vectors():
    """Эмбеддинги «запросов» этапов и подэтапов каталога. Кэш на время процесса.
    ponytail: сброс кэша — рестарт или reset_stage_vectors(); каталог меняется редко."""
    global _CATALOG_STAGE_VECS
    if _CATALOG_STAGE_VECS is None:
        import planner
        cat = planner.load_catalog()
        sv, subv = [], []
        for st in cat.get("stages") or []:
            sv.append((st["id"], get_embedding(planner.catalog_stage_query(st))))
            for sub in st.get("substage_templates") or []:
                subv.append((st["id"], sub["id"], get_embedding(planner.catalog_substage_query(st, sub))))
        _CATALOG_STAGE_VECS = (sv, subv)
    return _CATALOG_STAGE_VECS


def reset_stage_vectors():
    global _CATALOG_STAGE_VECS
    _CATALOG_STAGE_VECS = None


def _stage_tags(vec):
    """(plan_stages[], plan_substages[]) для вектора чанка. Привязка ОТНОСИТЕЛЬНАЯ
    (см. _select_substages): 0–3 самых близких подэтапа, а не всё подряд по порогу.
    Этапы выводятся из принятых подэтапов."""
    _, subv = _catalog_stage_vectors()
    scored = [(stid, sub_id, _cos(vec, sv)) for stid, sub_id, sv in subv]
    accepted = _select_substages(scored)
    subs = sorted({s for _, s, _ in accepted})
    stages = sorted({st for st, _, _ in accepted})
    return stages, subs


def assign_chunks_to_stages() -> dict:
    """Раскладывает содержательные чанки по этапам И подэтапам каталога адаптации: каждому
    чанку проставляет payload.plan_stages и payload.plan_substages — id, смыслу которых он
    соответствует (по близости к «запросу этапа/подэтапа»). Мульти-лейбл: один чанк (и один
    документ) может относиться к нескольким этапам и подэтапам. Служебный мусор (meaningful=False)
    пропускается — метки пустые. Дёшево: эмбеддинги этапов/подэтапов + косинус к готовым векторам."""
    reset_stage_vectors()                       # каталог мог измениться — пересчитать векторы
    stage_vecs, sub_vecs = _catalog_stage_vectors()
    for field in ("plan_stages", "plan_substages"):
        try:
            client.create_payload_index(COLLECTION_NAME, field_name=field,
                                         field_schema=PayloadSchemaType.KEYWORD)
        except Exception:
            pass

    offset = None
    touched = 0
    while True:
        batch, offset = client.scroll(
            collection_name=COLLECTION_NAME, limit=256,
            with_payload=True, with_vectors=True, offset=offset,
        )
        for p in batch:
            if (p.payload or {}).get("meaningful") is False:   # мусор не распределяем
                client.set_payload(collection_name=COLLECTION_NAME,
                                   payload={"plan_stages": [], "plan_substages": []}, points=[p.id])
                touched += 1
                continue
            plan_stages, plan_substages = _stage_tags(p.vector)
            client.set_payload(collection_name=COLLECTION_NAME,
                               payload={"plan_stages": plan_stages, "plan_substages": plan_substages},
                               points=[p.id])
            touched += 1
        if offset is None:
            break
    _log("STAGES", f"чанков разложено: {touched} (этапов {len(stage_vecs)}, подэтапов {len(sub_vecs)})")
    return {"chunks": touched, "stages": len(stage_vecs), "substages": len(sub_vecs)}


def _vector_stats(vector) -> dict:
    """Компактная сводка по вектору чанка: размерность, норма, первые значения.
    Полный вектор из 1024 чисел в UI не нужен — для инспекции достаточно превью."""
    vec = list(vector or [])
    dim = len(vec)
    norm = math.sqrt(sum(v * v for v in vec)) if vec else 0.0
    return {
        "dim": dim,
        "norm": round(norm, 4),
        "preview": [round(float(v), 4) for v in vec[:16]],
    }


def get_document_chunks(filename: str) -> Optional[dict]:
    """
    Подробности индексации одного документа для админки: как он разбит на чанки
    и как выглядит вектор каждого чанка. None — если чанков в Qdrant нет.
    """
    points = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=filename))]),
            limit=256,
            with_payload=True,
            with_vectors=True,
            offset=offset,
        )
        points.extend(batch)
        if offset is None:
            break

    if not points:
        return None

    chunks = []
    for p in points:
        payload = p.payload or {}
        chunks.append({
            "id": str(p.id),
            "chunk_index": payload.get("chunk_index"),
            "section": payload.get("section"),
            "headings": payload.get("headings") or [],
            "page": payload.get("page"),
            "meaningful": payload.get("meaningful", True),
            "folders": payload.get("folders") or [],
            "stage_ids": payload.get("stage_ids") or [],
            "plan_stages": payload.get("plan_stages") or [],
            "plan_substages": payload.get("plan_substages") or [],
            "profession": payload.get("profession") or "",
            "length": payload.get("length"),
            "text": payload.get("text", ""),          # то, что реально ушло в эмбеддинг
            "raw_text": payload.get("raw_text", ""),   # исходный текст пункта без контекста заголовков
            "vector": _vector_stats(p.vector),
        })
    chunks.sort(key=lambda c: (c["chunk_index"] is None, c["chunk_index"] or 0))
    return {"filename": filename, "chunks": chunks, "count": len(chunks)}


def document_substage_map(filename: str) -> Optional[dict]:
    """Разбивка документа по подэтапам с ОБОСНОВАНИЕМ: для каждого содержательного чанка —
    к каким подэтапам он отнесён и НАСКОЛЬКО близок (косинус к «запросу подэтапа» каталога).
    Score — и есть критерий: чем выше, тем увереннее; низкий у пункта → вероятно, попал ошибочно.
    Возвращает {filename, chunks:[{chunk_index, section, page, text, meaningful,
    matches:[{substage_id, stage_id, stage_title, title, brief, score}]}]}."""
    import planner
    cat = planner.load_catalog()
    meta = {}   # sub_id -> подписи для UI
    for st in cat.get("stages") or []:
        for sub in st.get("substage_templates") or []:
            meta[sub["id"]] = {"stage_id": st["id"], "stage_title": st.get("title", ""),
                               "title": sub.get("title", ""), "brief": sub.get("brief", "")}
    _, sub_vecs = _catalog_stage_vectors()   # [(stage_id, sub_id, vec)]

    points = []
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=filename))]),
            limit=256, with_payload=True, with_vectors=True, offset=offset,
        )
        points.extend(batch)
        if offset is None:
            break
    if not points:
        return None

    chunks = []
    for p in points:
        pl = p.payload or {}
        meaningful = pl.get("meaningful", True)
        matches = []
        if meaningful:
            scored = [(stid, sub_id, _cos(p.vector, sv)) for stid, sub_id, sv in sub_vecs]
            accepted = {(st, sub) for st, sub, _ in _select_substages(scored)}   # реальные привязки
            for stid, sub_id, sc in scored:
                if sc >= PLAN_STAGE_MATCH:   # показываем и кандидатов — для ручной оценки
                    m = meta.get(sub_id, {})
                    matches.append({"substage_id": sub_id, "stage_id": stid,
                                    "stage_title": m.get("stage_title", ""), "title": m.get("title", ""),
                                    "brief": m.get("brief", ""), "score": round(sc, 3),
                                    "accepted": (stid, sub_id) in accepted})
            matches.sort(key=lambda x: (x["accepted"], x["score"]), reverse=True)
        chunks.append({
            "chunk_index": pl.get("chunk_index"),
            "section": pl.get("section") or "",
            "page": pl.get("page"),
            "meaningful": meaningful,
            "text": pl.get("raw_text") or pl.get("text") or "",
            "matches": matches,
        })
    chunks.sort(key=lambda c: (c["chunk_index"] is None, c["chunk_index"] or 0))
    return {"filename": filename, "threshold": PLAN_STAGE_MATCH, "chunks": chunks, "count": len(chunks)}


def get_folder_chunks(slug: str, limit: int = 1000) -> dict:
    """Чанки, отнесённые к смысловой папке (payload.folders содержит slug) — для просмотра
    содержимого папки в админке. Векторы не тянем (для просмотра не нужны, легче ответ).
    Отсортированы по документу и порядку чанка. limit — верхний предел (ponytail: для
    браузинга хватает; при очень больших папках покажем первые N и count=предел)."""
    points = []
    offset = None
    while len(points) < limit:
        batch, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="folders", match=MatchValue(value=slug))]),
            limit=min(256, limit - len(points)),
            with_payload=True, with_vectors=False, offset=offset,
        )
        points.extend(batch)
        if offset is None:
            break

    chunks = []
    for p in points:
        payload = p.payload or {}
        chunks.append({
            "id": str(p.id),
            "source": payload.get("source"),
            "chunk_index": payload.get("chunk_index"),
            "section": payload.get("section"),
            "page": payload.get("page"),
            "length": payload.get("length"),
            "folders": payload.get("folders") or [],
            "text": payload.get("text", ""),
        })
    chunks.sort(key=lambda c: ((c["source"] or ""), c["chunk_index"] is None, c["chunk_index"] or 0))
    return {"slug": slug, "chunks": chunks, "count": len(chunks), "truncated": len(points) >= limit}


def delete_document_vectors(filename: str):
    """Удаляет из Qdrant все точки данного документа (по полю source)."""
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=filename))]
        ),
    )


def delete_document(filename: str, remove_file: bool = True) -> bool:
    """Полное удаление документа: чанки из Qdrant + вектор описания + оригинал +
    markdown-версия + кэш + запись в реестре. Папки (логические категории) при этом
    не трогаются — удаляется сам документ, а не категория."""
    delete_document_vectors(filename)
    try:
        classify.delete_doc_summary(filename)
    except Exception as e:
        _log("DELETE", f"вектор описания {filename}: {e}")

    with _registry_lock:
        reg = _load_registry()
        entry = reg.get(filename)

    if entry and remove_file:
        rel_path = entry.get("path", filename)
        filepath = DOCS_DIR / rel_path
        if filepath.exists():
            filepath.unlink()
            for cache_file in CACHE_DIR.glob(f"{filepath.stem}__*.json"):
                cache_file.unlink()
        md_rel = entry.get("markdown_path")
        if md_rel:
            md_path = CONVERTED_DIR / md_rel
            if md_path.exists():
                md_path.unlink()

    with _registry_lock:
        reg = _load_registry()
        existed = reg.pop(filename, None) is not None
        _save_registry(reg)

    # Синхронно убираем строку из реестра метаданных, чтобы экран не показывал удалённое.
    try:
        documents.remove_by_filename(filename)
    except Exception as e:
        _log("DELETE", f"метаданные {filename} из БД: {e}")
    return existed


# ---------- Снятие метки папки с чанков (при удалении/выключении папки) ----------
def strip_folder_from_chunks(slug: str):
    """Убирает slug папки из payload всех чанков и из реестра документов. Документы
    остаются в общей базе — удаляется только принадлежность к категории (ТЗ §3)."""
    offset = None
    while True:
        batch, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="folders", match=MatchValue(value=slug))]),
            limit=256, with_payload=True, offset=offset,
        )
        for p in batch:
            payload = p.payload or {}
            new_folders = [s for s in (payload.get("folders") or []) if s != slug]
            client.set_payload(collection_name=COLLECTION_NAME, payload={"folders": new_folders},
                               points=[p.id])
        if offset is None:
            break
    with _registry_lock:
        reg = _load_registry()
        for doc in reg.values():
            if slug in (doc.get("folders") or []):
                doc["folders"] = [s for s in doc["folders"] if s != slug]
        _save_registry(reg)


# ---------- Приём загруженного файла (используется веб-ручкой upload) ----------
def save_uploaded_file(filename: str, content: bytes) -> Path:
    filename = safe_filename(filename)
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"Неподдерживаемый формат файла: {ext or '(нет расширения)'}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024*1024)} МБ")

    # Файл хранится в одном экземпляре, плоско в data/documents/. Папки — логические
    # метки, физических копий не создают (ТЗ §2). Классификацию по папкам сделает
    # index_document() при обработке.
    filepath = DOCS_DIR / filename
    with open(filepath, "wb") as f:
        f.write(content)

    _update_registry(
        filename,
        size_bytes=len(content),
        uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        status="uploaded",
        chunks=0,
        folders=[],
        stage_ids=[],
        summary=None,
        error=None,
    )
    return filepath


# ---------- Фоновая очередь индексации (используется веб-ручкой upload) ----------
# docling-разбор тяжёлый (layout PDF — секунды-минуты), поэтому загрузка через сайт
# не ждёт обработку синхронно, а ставит файл в очередь. Один воркер обрабатывает файлы
# по очереди (FIFO): параллельный docling на нескольких файлах съел бы всю память.
# ponytail: один глобальный воркер; если понадобится пропускная способность —
# несколько воркеров + семафор под память.
_index_queue: "queue.Queue[str]" = queue.Queue()
_index_jobs: dict = {}
_index_jobs_lock = threading.Lock()
_worker_started = False
_worker_lock = threading.Lock()


def _set_index_job(job_id: str, **fields):
    with _index_jobs_lock:
        job = _index_jobs.setdefault(job_id, {"job_id": job_id})
        job.update(fields)
        return dict(job)


def get_index_job(job_id: str) -> Optional[dict]:
    with _index_jobs_lock:
        job = _index_jobs.get(job_id)
        return dict(job) if job else None


def queue_status() -> dict:
    with _index_jobs_lock:
        jobs = list(_index_jobs.values())
    queued = sum(1 for j in jobs if j.get("status") == "queued")
    running = sum(1 for j in jobs if j.get("status") == "processing")
    return {"queued": queued, "running": running}


def _index_worker():
    while True:
        job_id = _index_queue.get()
        try:
            job = get_index_job(job_id)
            if not job:
                continue
            filepath = Path(job["filepath"])
            _set_index_job(job_id, status="processing", started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            _update_registry(filepath.name, status="processing", error=None)
            result = index_document(filepath)
            _set_index_job(job_id, status=result["status"], result=result,
                           finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception as e:
            _set_index_job(job_id, status="error", result={"status": "error", "error": str(e)})
        finally:
            _index_queue.task_done()


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_index_worker, name="index-worker", daemon=True).start()
        _worker_started = True


def enqueue_document(filepath: Path) -> dict:
    """Ставит уже сохранённый файл в очередь на индексацию. Возвращает запись задачи."""
    _ensure_worker()
    job_id = str(uuid.uuid4())
    filename = Path(filepath).name
    _set_index_job(job_id, filename=filename, filepath=str(filepath),
                   status="queued", queued_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                   position=_index_queue.qsize() + 1)
    _index_queue.put(job_id)
    return get_index_job(job_id)


# ---------- Массовая индексация папки (используется CLI-скриптом) ----------
def index_all_documents(docs_dir: Optional[Path] = None, recreate: bool = False):
    docs_dir = Path(docs_dir) if docs_dir else DOCS_DIR
    create_collection(recreate=recreate)

    # Рекурсивно: файлы и в корне (ещё не отсортированы), и уже разложенные по
    # подпапкам тем (вручную или из прошлого запуска) — index_document() сам
    # разбирается, что с чем делать.
    files = sorted(
        p for p in docs_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT and CACHE_DIR not in p.parents
    )
    if not files:
        print(f"В папке {docs_dir} не найдено поддерживаемых файлов ({', '.join(sorted(SUPPORTED_EXT))}).")
        return

    # Векторы папок должны существовать до классификации чанков.
    try:
        classify.sync_folder_vectors()
    except Exception as e:
        print(f"Предупреждение: не удалось построить векторы папок: {e}")

    for filepath in files:
        if filepath.name not in _load_registry():
            _update_registry(
                filepath.name,
                size_bytes=filepath.stat().st_size,
                uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                status="uploaded",
                chunks=0,
                folders=[],
                stage_ids=[],
                error=None,
            )
        print(f"\n=== Обработка файла: {filepath.name} ===")
        result = index_document(filepath)
        if result["status"] == "indexed":
            print(f"  Загружено {result['chunks']} чанков за {result['elapsed']:.2f} сек -> папки: {result['folders'] or '(общая база)'}")
        else:
            print(f"  ОШИБКА: {result.get('error')}")

    print("\nИндексация завершена.")


# ---------- Повторный анализ (ТЗ §8, §26) ----------
def reanalyze_document(filename: str) -> dict:
    """Заново классифицирует уже проиндексированный документ БЕЗ повторного docling/
    эмбеддинга: перечитывает чанки из Qdrant, гоняет их через classify.* и обновляет
    метки папок/этапов. Дёшево — векторы уже есть.

    Статус в реестре ведём по ходу дела (reanalyzing -> indexed/error), чтобы он был
    виден в списке документов рядом с каждым документом при переанализе."""
    _update_registry(filename, status="reanalyzing", error=None)
    try:
        with _registry_lock:
            entry = _load_registry().get(filename)
        summary = (entry or {}).get("summary") or ""
        doc_cls = classify.classify_document(summary) if summary else {"folders": [], "stage_ids": []}
        doc_folders = doc_cls["folders"]
        # Профессия документа стабильна (задана при индексации) — берём из реестра, не гоняем
        # LLM. Переанализ реагирует на изменения ПАПОК/каталога, а не на профессию.
        doc_profession = (entry or {}).get("profession") or ""

        offset = None
        touched = 0
        while True:
            batch, offset = client.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=filename))]),
                limit=256, with_payload=True, with_vectors=True, offset=offset,
            )
            for p in batch:
                base_text = (p.payload or {}).get("raw_text") or (p.payload or {}).get("text") or ""
                meaningful = classify.is_meaningful(base_text)
                if meaningful:
                    cc = classify.classify_chunk(base_text, doc_folders, vec=p.vector, confirm=False)
                    # Профессия чанка проставлена при индексации и стабильна — не гоняем LLM
                    # на каждый чанк (это делало переанализ 78-чанкового документа многочасовым).
                    # Переанализ реагирует на изменения папок/каталога — это векторные операции.
                    chunk_prof = (p.payload or {}).get("profession") or ""
                    plan_stages, plan_substages = _stage_tags(p.vector)
                else:
                    cc = {"folders": [], "stage_ids": []}
                    chunk_prof = ""
                    plan_stages, plan_substages = [], []
                client.set_payload(collection_name=COLLECTION_NAME,
                                   payload={"meaningful": meaningful, "folders": cc["folders"],
                                            "stage_ids": cc["stage_ids"], "profession": chunk_prof,
                                            "plan_stages": plan_stages, "plan_substages": plan_substages},
                                   points=[p.id])
                touched += 1
            if offset is None:
                break
        _update_registry(filename, folders=doc_folders, stage_ids=doc_cls["stage_ids"],
                         profession=doc_profession, status="indexed", error=None)
        return {"filename": filename, "folders": doc_folders, "chunks": touched}
    except Exception as e:
        _log("REANALYZE", f"{filename}: ошибка переанализа ({e})")
        _update_registry(filename, status="error", error=str(e))
        return {"filename": filename, "error": str(e)}


def reanalyze_all() -> dict:
    """Полный повторный анализ всей базы (ТЗ §8 — «запустить повторный анализ всей базы»)."""
    classify.sync_folder_vectors()
    results = []
    for doc in list_documents():
        if doc.get("status") == "indexed":
            results.append(reanalyze_document(doc["filename"]))
    return {"reanalyzed": len(results), "documents": results}


def reanalyze_for_folder(slug: str) -> dict:
    """Точечный реанализ под изменившуюся/новую папку (ТЗ §8): по вектору папки
    находим потенциально релевантные документы и переанализируем только их, а не всю базу."""
    classify.sync_folder_vectors()
    folder = folders.get_by_slug(slug)
    if not folder:
        return {"reanalyzed": 0, "documents": []}
    # Кандидаты — документы, чьи чанки близки к вектору папки (+ уже помеченные ею).
    candidates = set()
    try:
        vec = get_embedding(classify.folder_tag_text(folder))
        hits = client.query_points(collection_name=COLLECTION_NAME, query=vec,
                                   limit=200, with_payload=True).points
        for h in hits:
            src = (h.payload or {}).get("source")
            if src:
                candidates.add(src)
    except Exception as e:
        _log("REANALYZE", f"векторный отбор кандидатов не удался ({e}) — беру все документы")
        candidates = {d["filename"] for d in list_documents() if d.get("status") == "indexed"}
    for doc in list_documents():
        if slug in (doc.get("folders") or []):
            candidates.add(doc["filename"])
    results = [reanalyze_document(name) for name in sorted(candidates)]
    return {"reanalyzed": len(results), "documents": results}
