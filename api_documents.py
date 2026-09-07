"""База знаний: загрузка и разбор документов, смысловые папки, хранилище оригиналов."""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from fastapi.responses import JSONResponse

import config
import storage
import indexing
import docview
import documents
import folders
import stages
import classify
import users
from config import MAX_UPLOAD_BYTES
from deps import _bg, require_admin, admin_only, owner_only, can_see_doc, visible_documents, ensure_doc_access

router = APIRouter()


# ---------- Управление документами ----------
@router.get("/documents")
async def get_documents(user: dict = Depends(require_admin)):
    """Список документов в базе с их статусом индексации (загружен / обрабатывается / готов / ошибка).
    Суперадмину — все документы, администратору — только его собственные загрузки."""
    return {"documents": visible_documents(user),
            "scope": "all" if users.is_owner(user) else "own"}


@router.get("/documents/board")
async def get_documents_board(user: dict = Depends(require_admin)):
    """Данные экрана «этапы ↔ документы»: этапы, подэтапы и относящиеся к ним
    документы (по метаданным из реестра) + документы без уверенной привязки.
    Администратор видит на доске только свои документы."""
    return documents.build_board(stages.list_stages(),
                                 [d for d in documents.list_docs() if can_see_doc(user, d)])


@router.get("/documents/table")
async def get_documents_table(user: dict = Depends(require_admin)):
    """Табличные данные по обработанным файлам из реестра метаданных (PostgreSQL):
    все поля, кроме тяжёлого вектора (у него — только длина). Фильтр по владельцу."""
    return {"documents": [d for d in documents.list_meta() if can_see_doc(user, d)]}


@router.get("/documents/{filename}/substage-map")
async def get_document_substage_map(filename: str, user: dict = Depends(require_admin)):
    """Разбивка документа по подэтапам: какие куски текста к каким подэтапам отнесены и
    с какой уверенностью (косинус) — критерий попадания. Низкий score выдаёт ошибочные."""
    ensure_doc_access(user, filename)
    data = docview.document_substage_map(filename)
    if data is None:
        raise HTTPException(status_code=404, detail="Чанки документа не найдены")
    return data


@router.post("/documents/{filename}/reindex")
async def reindex_document(filename: str, user: dict = Depends(require_admin)):
    """Переанализ документа (без повторного docling): обновляет папки/этапы по чанкам
    и синхронизирует запись в реестре метаданных."""
    ensure_doc_access(user, filename)
    # reanalyze_document сам синхронизирует document_meta (доску «этапы ↔ документы»),
    # поэтому отдельной досинхронизации здесь больше нет — один путь, без дрейфа.
    result = indexing.reanalyze_document(filename)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/documents/upload")
async def upload_document(file: UploadFile = File(...), user: dict = Depends(require_admin)):
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

    # Дедупликация по содержимому (sha256): тот же файл не грузим и не индексируем заново.
    try:
        existing = documents.find_by_hash(documents.hash_bytes(content))
    except Exception:
        existing = None   # реестр недоступен — не блокируем загрузку
    if existing:
        return JSONResponse(status_code=200, content={
            "duplicate": True, "filename": existing["filename"],
            "uploaded_at": existing.get("uploaded_at"),
            "message": f"Такой файл уже загружен ранее ({existing['filename']}) — повторная обработка не требуется",
        })

    try:
        # uploader -> владелец документа: задаёт путь <суперадмин>/<админ>/<файл>
        # в S3 и определяет, кому документ будет виден.
        filepath = indexing.save_uploaded_file(file.filename, content, uploader=user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = indexing.enqueue_document(filepath)

    # Точная разметка docpipe (двухпроходная LLM: карточка документа -> метки секций
    # по этапам/подэтапам/профессиям). Идёт параллельно фолдер-индексации для RAG.
    # Сбой разметки не должен блокировать загрузку/RAG.
    try:
        import docpipe
        job["label_job_id"] = docpipe.enqueue(filepath, file.filename)
    except Exception as e:
        print(f"[DOCPIPE] не удалось поставить разметку в очередь: {e}")
        job["label_job_id"] = None
    return JSONResponse(status_code=202, content=job)


@router.get("/api/s3/list")
async def api_s3_list(prefix: str = "", recursive: bool = False,
                      user: dict = Depends(require_admin)):
    """Листинг бакета (метаданные): «папки» + файлы уровня, либо рекурсивно. Хранилище
    может быть на другом сервере — endpoint/bucket отдаём в ответе.

    Суперадмин ходит по всему бакету (его папка — корень структуры), обычный
    администратор заперт в СВОЁМ подкаталоге <суперадмин>/<админ>/: запрошенный
    префикс вне него подменяется на собственный, чужие файлы не листаются."""
    import config
    import storage
    home = ""
    if not users.is_owner(user):
        top, own = indexing.owner_dirs(user)
        home = f"{config.S3_PREFIX}{top}/{own}/"
        if not (prefix or "").startswith(home):
            prefix = home
    data = storage.list_objects(prefix=prefix, delimiter=("" if recursive else "/"))
    data["home"] = home          # ниже этого префикса администратору спускаться нельзя
    data["scope"] = "all" if users.is_owner(user) else "own"
    return data


@router.get("/documents/jobs/{job_id}", dependencies=admin_only)
async def get_document_job(job_id: str):
    """Статус фоновой индексации загруженного документа."""
    job = indexing.get_index_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job


@router.get("/documents/label-jobs/{job_id}", dependencies=admin_only)
async def get_label_job(job_id: str):
    """Статус фоновой LLM-разметки docpipe (проход по секциям, возобновляемый)."""
    import docpipe
    job = docpipe.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача разметки не найдена")
    return dict(job)


@router.get("/documents/labeled")
async def list_labeled_documents(user: dict = Depends(require_admin)):
    """Имена документов, размеченных docpipe (для просмотрщика разбора). Только свои."""
    import docpipe.store as dstore
    allowed = {d.get("filename") for d in visible_documents(user)}
    return {"documents": [f for f in dstore.list_documents() if f in allowed]}


@router.get("/documents/{filename}/labels")
async def get_document_labels(filename: str, user: dict = Depends(require_admin)):
    """Полный разбор документа (docpipe): карточка документа + блоки (секции) с текстом,
    метками (этапы/подэтапы/профессии), обоснованием «почему», названиями и описаниями
    этапов/подэтапов, и чанками каждого блока. Источник правды по разметке (LLM, temp=0)."""
    ensure_doc_access(user, filename)
    import docpipe
    data = docpipe.document_breakdown(filename)
    if data is None:
        raise HTTPException(status_code=404, detail="Документ не размечен docpipe (загрузите заново или дождитесь разметки)")
    return data


@router.get("/documents/{filename}/chunks")
async def get_document_chunks(filename: str, user: dict = Depends(require_admin)):
    """Подробности разбиения документа: чанки и вектор каждого чанка (для кнопки «Подробнее»)."""
    ensure_doc_access(user, filename)
    detail = docview.get_document_chunks(filename)
    if detail is None:
        raise HTTPException(status_code=404, detail="Чанки не найдены — документ ещё не проиндексирован")
    return detail


@router.delete("/documents/{filename}")
async def remove_document(filename: str, user: dict = Depends(require_admin)):
    """Удаляет документ: векторы из Qdrant, оригинал из data/documents, кэш docling."""
    ensure_doc_access(user, filename)
    existed = indexing.delete_document(filename)
    if not existed:
        raise HTTPException(status_code=404, detail="Документ не найден")
    try:
        documents.remove_by_filename(filename)
    except Exception:
        pass
    return {"filename": filename, "deleted": True}


# ---------- Смысловые папки (логические категории; ими управляет человек) ----------
class FolderRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    criteria: list | None = None
    stage_ids: list | None = None
    enabled: bool | None = None


@router.get("/folders", dependencies=admin_only)
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


@router.get("/folders/{slug}/chunks", dependencies=admin_only)
async def get_folder_chunks(slug: str):
    """Чанки внутри смысловой папки — просмотр содержимого папки (текст + из какого документа)."""
    return docview.get_folder_chunks(slug)


@router.post("/folders", dependencies=admin_only)
def create_folder(req: FolderRequest):
    try:
        folder = folders.create_folder(req.name, req.description or "", req.criteria, req.stage_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Новая папка -> обновляем векторы и переанализируем потенциально релевантные
    # документы, чтобы они попали в неё (ТЗ §8).
    classify.sync_folder_vectors()
    _bg(indexing.reanalyze_for_folder, folder["slug"])
    return folder


@router.put("/folders/{folder_id}", dependencies=admin_only)
def update_folder(folder_id: str, req: FolderRequest):
    existing = folders.get_folder(folder_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    try:
        folder = folders.update_folder(folder_id, **req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    classify.sync_folder_vectors()
    # Изменились критерии/название/этапы или папку включили — переанализируем документы.
    fields = req.model_dump(exclude_none=True)
    if any(k in fields for k in ("name", "description", "criteria", "stage_ids", "enabled")):
        _bg(indexing.reanalyze_for_folder, folder["slug"])
    return folder


@router.delete("/folders/{folder_id}", dependencies=admin_only)
def delete_folder(folder_id: str):
    """Удаляет только логическую категорию — документы остаются в общей базе знаний."""
    folder = folders.get_folder(folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    folders.delete_folder(folder_id)
    indexing.strip_folder_from_chunks(folder["slug"])   # снимаем метку с чанков, документы не трогаем
    classify.sync_folder_vectors()
    return {"deleted": True}


# ---------- Повторный анализ и уточнения по документам (ТЗ §8, §16, §26) ----------
class ClarifyRequest(BaseModel):
    clarification: str


@router.post("/documents/reanalyze", dependencies=owner_only)
def reanalyze_documents():
    """Полный повторный анализ ВСЕЙ базы под текущую структуру папок (фоново).
    Только суперадмин: операция задевает документы всех администраторов."""
    classify.sync_folder_vectors()
    _bg(indexing.reanalyze_all)
    return {"started": True}


@router.post("/documents/{filename}/reanalyze")
def reanalyze_one(filename: str, user: dict = Depends(require_admin)):
    """Переанализ одного документа — фоново; статус (reanalyzing -> indexed/error)
    виден в списке документов рядом с этим документом."""
    ensure_doc_access(user, filename)
    _bg(indexing.reanalyze_document, filename)
    return {"started": True}


@router.post("/documents/{filename}/clarify")
async def clarify_document(filename: str, req: ClarifyRequest,
                           user: dict = Depends(require_admin)):
    """Текстовое уточнение пользователя (актуальность/архив/область действия — ТЗ §17).
    Исходный документ не переписывается — уточнение хранится как доп. контекст."""
    ensure_doc_access(user, filename)
    if not indexing.set_clarification(filename, req.clarification):
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {"filename": filename, "clarification": req.clarification}
