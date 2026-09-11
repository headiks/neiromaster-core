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
import storage
import docregistry
from config import (
    DOCS_DIR, CONVERTED_DIR, CACHE_DIR,
    SUPPORTED_EXT, MAX_UPLOAD_BYTES,
    QDRANT_HOST, QDRANT_PORT, EMBED_DIM, get_embedding, cosine,
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
def _log(step, msg):
    print(f"[INDEX:{step}] {msg}")


# ---------- Реестр документов ----------
# Само хранилище вынесено в docregistry.py (файловый store под блокировкой). Здесь —
# только реэкспорт публичных имён (indexing.list_documents и т.п. зовут снаружи).
_update_registry = docregistry.update
_load_registry = docregistry.load
list_documents = docregistry.list_documents
set_clarification = docregistry.set_clarification
folder_doc_counts = docregistry.folder_doc_counts


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
def _detect_rotated_pages(path: Path) -> dict:
    """{индекс_страницы: угол_текста°} для страниц, где текст нарисован боком
    (ландшафтные таблицы без флага /Rotate). Угол — из pdfium (FPDFText_GetCharAngle,
    против часовой). Ровные страницы (0°) в результат не попадают."""
    import math
    import statistics
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    out = {}
    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(pdf)):
            tp = pdf[i].get_textpage()
            try:
                n = tp.count_chars()
                if n < 20:          # мало текста — угол ненадёжен, пропускаем
                    continue
                step = max(1, n // 200)   # выборка символов, не весь текст
                angs = [pdfium_c.FPDFText_GetCharAngle(tp.raw, k) for k in range(0, n, step)]
                angs = [a for a in angs if a is not None and a >= 0]
                if not angs:
                    continue
                deg = round(math.degrees(statistics.median(angs))) % 360
                if deg in (90, 270):
                    out[i] = deg
            finally:
                tp.close()
    finally:
        pdf.close()
    return out


def _normalize_rotation(filepath: Path):
    """Разворачивает страницы с боком-нарисованным текстом, чтобы docling читал их
    правильно (иначе таблица приходит перемешанной). Возвращает (путь_для_docling,
    временный_ли_файл). Не-PDF и любой сбой -> исходный файл (индексацию не роняем)."""
    if filepath.suffix.lower() != ".pdf":
        return filepath, False
    try:
        rotated = _detect_rotated_pages(filepath)
        if not rotated:
            return filepath, False
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(str(filepath))
        writer = PdfWriter()
        for i, page in enumerate(reader.pages):
            if i in rotated:
                page.rotate((360 - rotated[i]) % 360)   # компенсируем угол текста
            writer.add_page(page)
        tmp = CACHE_DIR / f"{filepath.stem}__norot_{file_hash(filepath)}.pdf"
        with open(tmp, "wb") as f:
            writer.write(f)
        print(f"[index] {filepath.name}: развёрнуто повёрнутых страниц: {len(rotated)}")
        return tmp, True
    except Exception as e:
        print(f"[index] нормализация поворота не удалась ({filepath.name}): {e}")
        return filepath, False


def convert_document(filepath: Path) -> DoclingDocument:
    """
    Гоняем файл через docling. Разбор PDF/DOCX (особенно PDF с layout-моделью)
    — самая тяжёлая часть пайплайна, поэтому результат кэшируется в JSON:
    при повторной обработке того же файла (переезд между папками, смена
    параметров чанкинга) можно переиспользовать готовую структуру документа.
    """
    # если локальной копии нет — тянем оригинал из S3 по ключу из реестра
    # (структура <суперадмин>/<админ>/<файл>; у старых записей ключа нет — плоский)
    storage.pull(filepath, _load_registry().get(filepath.name, {}).get("s3_key") or "")
    cache_path = CACHE_DIR / f"{filepath.stem}__{file_hash(filepath)}.json"
    if cache_path.exists():
        return DoclingDocument.load_from_json(str(cache_path))

    # Повёрнутые страницы (боком-нарисованные таблицы) выправляем до docling.
    src, is_tmp = _normalize_rotation(filepath)
    try:
        result = converter.convert(str(src))
    finally:
        if is_tmp:
            try:
                src.unlink()
            except OSError:
                pass
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

        # ---- Классификация документа в существующие папки (гибрид A/B/C, ТЗ 1.4) ----
        doc_cls = classify.classify_document(summary, full_text=markdown_text)
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
            folder_reasons=doc_cls.get("decisions") or [],   # «почему» (метод/score/критерий) — ТЗ 1.4
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
        section_counters = {}   # раздел -> счётчик чанков внутри него (для стабильного ID)
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
                # Стабильный ID = hash(файл + путь раздела + № внутри раздела). Не зависит от
                # содержимого файла -> переиндексация даёт ТЕ ЖЕ ID (обновление, не дубли). ТЗ 1.3.
                sec_n = section_counters.get(section, 0)
                section_counters[section] = sec_n + 1
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{filename}|{section}|{sec_n}"))
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
                        "section_seq": sec_n,
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
    scored = [(stid, sub_id, cosine(vec, sv)) for stid, sub_id, sv in subv]
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

    entry = docregistry.get(filename)

    if entry and remove_file:
        rel_path = entry.get("path", filename)
        filepath = DOCS_DIR / rel_path
        # убираем оригинал из S3 по ключу владельца (no-op, если S3 выключен)
        storage.delete(rel_path, entry.get("s3_key") or "")
        if filepath.exists():
            filepath.unlink()
            for cache_file in CACHE_DIR.glob(f"{filepath.stem}__*.json"):
                cache_file.unlink()
        md_rel = entry.get("markdown_path")
        if md_rel:
            md_path = CONVERTED_DIR / md_rel
            if md_path.exists():
                md_path.unlink()

    existed = docregistry.remove(filename)

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
    docregistry.strip_folder(slug)


# ---------- Приём загруженного файла (используется веб-ручкой upload) ----------
def owner_dirs(uploader: Optional[dict] = None) -> tuple:
    """
    Пара папок хранилища для загрузки: (папка суперадмина, папка загрузившего).
    Структура в S3 — <суперадмин>/<администратор>/<файл>: всё, что грузят
    администраторы, лежит внутри папки суперадмина. Если загрузивший неизвестен
    (CLI-индексация), кладём в папку суперадмина как в общую.
    """
    import users
    top = users.dir_slug(users.get_owner())
    return top, (users.dir_slug(uploader) if uploader else top)


def save_uploaded_file(filename: str, content: bytes, uploader: Optional[dict] = None) -> Path:
    filename = safe_filename(filename)
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"Неподдерживаемый формат файла: {ext or '(нет расширения)'}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Файл превышает лимит {MAX_UPLOAD_BYTES // (1024*1024)} МБ")

    # Локально файл лежит плоско в data/documents/ — это КЭШ для конвейера docling
    # (папки — логические метки, физических копий не создают, ТЗ §2). Структура
    # «суперадмин/администратор» живёт в durable-хранилище S3: см. storage.doc_key.
    filepath = DOCS_DIR / filename
    with open(filepath, "wb") as f:
        f.write(content)
    try:
        top, own = owner_dirs(uploader)
    except Exception as e:            # БД недоступна — не срываем загрузку
        _log("STORAGE", f"не удалось определить папку владельца: {e}")
        top = own = ""
    s3_key = storage.doc_key(filename, top, own)
    storage.put(filename, content, s3_key)   # дублируем оригинал в S3 (no-op, если S3 выключен)

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
        # Владелец документа: по нему работает разграничение видимости (админ видит
        # только свои загрузки, суперадмин — все) и строится путь в хранилище.
        uploaded_by=(uploader or {}).get("id"),
        uploaded_by_name=(uploader or {}).get("full_name") or (uploader or {}).get("username") or "",
        uploaded_by_role=(uploader or {}).get("role"),
        department=(uploader or {}).get("department") or "",
        storage_path=f"{top}/{own}/{filename}" if top and own else filename,
        s3_key=s3_key,
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


def requeue_stranded() -> int:
    """Возвращает в очередь документы, зависшие в 'uploaded'/'processing'. Очередь — в
    памяти процесса, поэтому рестарт приложения (или потерянная задача) оставлял бы такие
    файлы навсегда «Загружен», не индексируя. Зовётся при старте. Возвращает число."""
    n = 0
    for entry in _load_registry().values():
        if entry.get("status") in ("uploaded", "processing"):
            enqueue_document(DOCS_DIR / entry["filename"])
            n += 1
    if n:
        print(f"[index] возвращено в очередь зависших документов: {n}")
    return n


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
        entry = docregistry.get(filename)
        summary = (entry or {}).get("summary") or ""
        # Полный текст для сигнатур уровня A (если сохранённый markdown доступен).
        full_text = ""
        mdp = (entry or {}).get("markdown_path")
        if mdp:
            mdf = CONVERTED_DIR / mdp
            if mdf.exists():
                full_text = mdf.read_text(encoding="utf-8", errors="ignore")
        doc_cls = (classify.classify_document(summary, full_text=full_text) if summary
                   else {"folders": [], "stage_ids": [], "decisions": []})
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
                         folder_reasons=doc_cls.get("decisions") or [],
                         profession=doc_profession, status="indexed", error=None)
        # Синхронизируем доску «этапы ↔ документы» (document_meta) ЗДЕСЬ, а не в веб-слое:
        # так и синхронный /reindex, и фоновый /reanalyze обновляют её одинаково — общие
        # поля folders/stage_ids не разъезжаются между реестром и метаданными.
        try:
            documents.update_assignment_by_filename(filename, doc_folders, doc_cls["stage_ids"])
        except Exception as e:
            _log("REANALYZE", f"синхронизация метаданных {filename}: {e}")
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
