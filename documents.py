"""
documents.py — единый реестр метаданных загруженных документов в PostgreSQL.

Зачем (задача от руководителя):
  - одна БД «паспортов» файлов вместо файлового registry.json;
  - дедупликация по sha256: тот же файл не грузим и не переиндексируем дважды;
  - на этих метаданных строится экран «этапы/подэтапы ↔ документы».

Что хранит одна строка (таблица documents):
  sha256 (ключ) · имя · размер · тип · статус · когда/кем загружен ·
  краткое описание · ключевые слова · эмбеддинг-вектор ·
  папки · этапы (stage_ids) · подэтапы [{stage_id, substage_id, score}].

Вариант кластеризации — №1 (по смыслу, через эмбеддинги):
  документ относится к этапу/подэтапу по СХОДСТВУ его вектора с текстом
  этапа/подэтапа. Привязку к папкам и этапам уже делает classify.py — здесь
  добавлен только более тонкий уровень: конкретный ПОДЭТАП.

Эмбеддинг храним как нативный REAL[] Postgres (без расширения pgvector): для
Варианта 1 близость считаем в Python, а вектор в БД лежит на будущее (быстрый
поиск/перекластеризация — там уже пригодится pgvector). Это точка апгрейда.

Тяжёлые зависимости (db, Ollama, classify, qdrant) импортируются ВНУТРИ функций:
чистая логика (косинус, группировка экрана) наверху тестируется без БД и сети.
"""

import math
import time
from typing import Optional

# Имя таблицы РЕЕСТРА метаданных. НЕ "documents": так называется таблица пайплайна
# docpipe (со своей схемой и FK). Разводим по разным таблицам, чтобы обе жили рядом.
TABLE = "document_meta"

# Порог косинусной близости, при котором документ считаем относящимся к подэтапу.
# Ниже — документ привязан к этапу, но не к конкретному подэтапу (или ни к чему).
SUBSTAGE_THRESHOLD = 0.35


# ---------- Чистая логика (без БД и сети — тестируется отдельно) ----------
def cosine(a: list, b: list) -> float:
    """Косинусная близость двух векторов; 0.0 при пустом/нулевом."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def assign_substages(doc_vec: list, substage_vecs: list,
                     threshold: float = SUBSTAGE_THRESHOLD) -> list:
    """
    Относит документ к подэтапам по сходству вектора.
    substage_vecs — [{stage_id, substage_id, title, vec}].
    Возвращает лучший подэтап В КАЖДОМ этапе, если он прошёл порог:
        [{stage_id, substage_id, title, score}] по убыванию score.
    (Один документ может лечь в несколько этапов, но в каждом — в один, самый близкий подэтап.)
    """
    best_per_stage: dict = {}
    for s in substage_vecs:
        score = cosine(doc_vec, s.get("vec") or [])
        if score < threshold:
            continue
        cur = best_per_stage.get(s["stage_id"])
        if cur is None or score > cur["score"]:
            best_per_stage[s["stage_id"]] = {
                "stage_id": s["stage_id"], "substage_id": s["substage_id"],
                "title": s.get("title", ""), "score": round(score, 3),
            }
    return sorted(best_per_stage.values(), key=lambda x: x["score"], reverse=True)


def build_board(stages: list, docs: list) -> dict:
    """
    Собирает данные экрана: этапы -> подэтапы -> документы + «без привязки».
    Чистая функция: на вход — этапы (stages.list_stages) и документы (list_docs).

    stages: [{id, title, description, substages:[{id, title}]}]
    docs:   [{sha256, filename, mime, keywords, stage_ids, substages:[{stage_id, substage_id, score}], ...}]
    """
    def ref(d: dict) -> dict:
        return {
            "sha256": d.get("sha256"), "filename": d.get("filename"),
            "mime": d.get("mime"), "keywords": d.get("keywords") or [],
            "status": d.get("status"),
        }

    # индекс: (stage_id, substage_id) -> [док], и (stage_id, None) -> прикреплён к этапу без подэтапа
    sub_docs: dict = {}
    stage_only: dict = {}
    assigned_ids = set()
    for d in docs:
        subs = d.get("substages") or []
        stage_ids = d.get("stage_ids") or []
        if subs:
            for a in subs:
                sub_docs.setdefault((a["stage_id"], a["substage_id"]), []).append({**ref(d), "score": a.get("score")})
                assigned_ids.add(d.get("sha256"))
        # этапы, куда документ отнесён, но без конкретного подэтапа
        subs_stage_ids = {a["stage_id"] for a in subs}
        for sid in stage_ids:
            if sid not in subs_stage_ids:
                stage_only.setdefault(sid, []).append(ref(d))
                assigned_ids.add(d.get("sha256"))

    out_stages = []
    sub_count = 0
    for st in stages:
        subs_out = []
        for sub in st.get("substages") or []:
            sub_count += 1
            subs_out.append({
                "id": sub["id"], "title": sub.get("title", ""),
                "documents": sub_docs.get((st["id"], sub["id"]), []),
            })
        out_stages.append({
            "id": st["id"], "title": st.get("title", ""),
            "description": st.get("description", ""),
            "substages": subs_out,
            "documents": stage_only.get(st["id"], []),   # в этапе, но без подэтапа
        })

    unassigned = [ref(d) for d in docs if d.get("sha256") not in assigned_ids]
    return {
        "stages": out_stages,
        "unassigned": unassigned,
        "stats": {
            "stages": len(stages), "substages": sub_count,
            "documents": len(docs), "unassigned": len(unassigned),
        },
    }


def extract_keywords(text: str, n: int = 6) -> list:
    """
    Простое выделение ключевых слов: самые частые значимые слова описания.
    ponytail: наивная частотность со стоп-словами; upgrade — ключевые слова от LLM
    в момент summarize_document, если понадобится точнее.
    """
    import re
    stop = {
        "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
        "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
        "бы", "по", "ее", "мне", "было", "вот", "от", "для", "о", "из", "ему",
        "или", "быть", "был", "него", "до", "вас", "нибудь", "уже", "ни", "этот",
        "того", "потому", "этого", "какой", "совсем", "ним", "здесь", "этом",
        "один", "почти", "мой", "тем", "чтобы", "нее", "были", "куда", "зачем",
        "всех", "можно", "при", "об", "документ", "документа", "также", "это",
    }
    words = re.findall(r"[а-яёa-z0-9]{4,}", (text or "").lower())
    freq: dict = {}
    for w in words:
        if w in stop:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:n]]


# ---------- Хранилище (PostgreSQL) ----------
CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    sha256       TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    size_bytes   BIGINT,
    mime         TEXT,
    status       TEXT NOT NULL DEFAULT 'indexed',
    uploaded_at  TEXT,
    uploaded_by  TEXT,
    summary      TEXT,
    keywords     TEXT[],
    embedding    REAL[],
    folders      TEXT[],
    stage_ids    TEXT[],
    substages    JSONB DEFAULT '[]'::jsonb,
    updated_at   TEXT
)
"""
# Быстрый поиск «документы этапа» для экрана.
CREATE_INDEX = f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_stage ON {TABLE} USING GIN (stage_ids)"


def init():
    """Создаёт таблицу и индекс, если их ещё нет. Идемпотентно — звать при старте."""
    import db
    db.execute(CREATE_TABLE)
    db.execute(CREATE_INDEX)


def find_by_hash(sha256: str) -> Optional[dict]:
    """Дедупликация: строка документа с таким содержимым или None. Главная проверка
    перед повторной загрузкой — «этот файл уже есть»."""
    import db
    return db.query(f"SELECT * FROM {TABLE} WHERE sha256 = %s", (sha256,), "one")


def get(sha256: str) -> Optional[dict]:
    import db
    return db.query(f"SELECT * FROM {TABLE} WHERE sha256 = %s", (sha256,), "one")


def list_docs() -> list:
    import db
    return db.query(f"SELECT * FROM {TABLE} ORDER BY uploaded_at DESC NULLS LAST", (), "all")


def list_meta() -> list:
    """Строки для табличного интерфейса — все поля, КРОМЕ тяжёлого вектора (у него
    отдаём только длину). Иначе на каждый документ ехало бы по 1024 числа."""
    import db
    return db.query(
        f"""SELECT sha256, filename, size_bytes, mime, status, uploaded_at, uploaded_by,
                   summary, keywords, folders, stage_ids, substages, updated_at,
                   array_length(embedding, 1) AS embedding_dim
            FROM {TABLE} ORDER BY uploaded_at DESC NULLS LAST""", (), "all")


def update_assignment_by_filename(filename: str, folders: list, stage_ids: list) -> None:
    """Обновляет привязку (папки/этапы/подэтапы) документа по имени файла — после
    переанализа. Подэтапы пересчитываются из новых stage_ids. Вектор не трогаем."""
    import db
    from psycopg.types.json import Json
    row = db.query(f"SELECT embedding FROM {TABLE} WHERE filename = %s", (filename,), "one")
    doc_vec = (row or {}).get("embedding")
    subs = assign_substages(doc_vec, _substage_vectors(stage_ids or None)) if doc_vec else []
    db.execute(
        f"""UPDATE {TABLE} SET folders=%s, stage_ids=%s, substages=%s, updated_at=%s
            WHERE filename=%s""",
        (folders or [], stage_ids or [], Json(subs), time.strftime("%Y-%m-%dT%H:%M:%S"), filename),
    )


def remove(sha256: str) -> None:
    import db
    db.execute(f"DELETE FROM {TABLE} WHERE sha256 = %s", (sha256,))


def remove_by_filename(filename: str) -> None:
    """Убрать строку по имени файла — при удалении документа из базы знаний."""
    import db
    db.execute(f"DELETE FROM {TABLE} WHERE filename = %s", (filename,))


def hash_bytes(content: bytes) -> str:
    """sha256-«отпечаток» содержимого (первые 16 hex). Тот же формат, что у
    indexing.file_hash, чтобы дедуп по хэшу совпадал с обеих сторон."""
    import hashlib
    return hashlib.sha256(content).hexdigest()[:16]


_UPSERT = f"""
INSERT INTO {TABLE}
    (sha256, filename, size_bytes, mime, status, uploaded_at, uploaded_by,
     summary, keywords, embedding, folders, stage_ids, substages, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (sha256) DO UPDATE SET
    filename=EXCLUDED.filename, size_bytes=EXCLUDED.size_bytes, mime=EXCLUDED.mime,
    status=EXCLUDED.status, uploaded_at=EXCLUDED.uploaded_at, uploaded_by=EXCLUDED.uploaded_by,
    summary=EXCLUDED.summary, keywords=EXCLUDED.keywords, embedding=EXCLUDED.embedding,
    folders=EXCLUDED.folders, stage_ids=EXCLUDED.stage_ids, substages=EXCLUDED.substages,
    updated_at=EXCLUDED.updated_at
"""


def upsert(meta: dict) -> dict:
    """Создаёт/обновляет строку документа по sha256. meta — поля таблицы (см. classify_full)."""
    import db
    from psycopg.types.json import Json
    db.execute(_UPSERT, (
        meta["sha256"], meta.get("filename", ""), meta.get("size_bytes"), meta.get("mime"),
        meta.get("status", "indexed"), meta.get("uploaded_at"), meta.get("uploaded_by"),
        meta.get("summary"), meta.get("keywords") or [], meta.get("embedding"),
        meta.get("folders") or [], meta.get("stage_ids") or [],
        Json(meta.get("substages") or []), time.strftime("%Y-%m-%dT%H:%M:%S"),
    ))
    return get(meta["sha256"])


# ---------- Привязка к подэтапам (Вариант 1: по эмбеддингам) ----------
_sub_vec_cache: dict = {}   # (substage_id, title) -> вектор; подэтапов немного


def _substage_vectors(stage_ids: Optional[list] = None) -> list:
    """
    [{stage_id, substage_id, title, vec}] для подэтапов заданных этапов (или всех).
    Вектор = эмбеддинг «Этап: … / Подэтап: …» (даём модели контекст этапа). Кэшируем.
    """
    import stages as stages_mod
    from config import get_embedding
    out = []
    for st in stages_mod.list_stages():
        if stage_ids is not None and st["id"] not in stage_ids:
            continue
        for sub in st.get("substages") or []:
            key = (sub["id"], sub.get("title", ""))
            vec = _sub_vec_cache.get(key)
            if vec is None:
                text = f"{st.get('title','')}. {sub.get('title','')}. {st.get('description','')}".strip()
                vec = get_embedding(text)
                _sub_vec_cache[key] = vec
            out.append({"stage_id": st["id"], "substage_id": sub["id"],
                        "title": sub.get("title", ""), "vec": vec})
    return out


def classify_full(summary: str, filename: str, sha256: str, *,
                  size_bytes: Optional[int] = None, mime: Optional[str] = None,
                  uploaded_by: Optional[str] = None, uploaded_at: Optional[str] = None) -> dict:
    """
    Полный расчёт метаданных документа для Варианта 1 и запись в БД:
      1) папки + этапы — существующей логикой classify.classify_document (по смыслу);
      2) подэтапы — сходство вектора документа с векторами подэтапов его этапов;
      3) ключевые слова — из описания; эмбеддинг документа — сохраняем.
    Возвращает записанную строку.
    """
    from config import get_embedding
    import classify

    doc_cls = classify.classify_document(summary)          # {folders, stage_ids, candidates}
    doc_vec = get_embedding(summary or filename)
    subs = assign_substages(doc_vec, _substage_vectors(doc_cls.get("stage_ids") or None))

    return upsert({
        "sha256": sha256, "filename": filename, "size_bytes": size_bytes, "mime": mime,
        "status": "indexed", "uploaded_at": uploaded_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "uploaded_by": uploaded_by, "summary": summary,
        "keywords": extract_keywords(summary), "embedding": doc_vec,
        "folders": doc_cls.get("folders") or [], "stage_ids": doc_cls.get("stage_ids") or [],
        "substages": subs,
    })


def record(sha256: str, filename: str, summary: str, folders: list, stage_ids: list, *,
           size_bytes: Optional[int] = None, mime: Optional[str] = None,
           uploaded_at: Optional[str] = None, uploaded_by: Optional[str] = None) -> dict:
    """
    Запись документа из пайплайна индексации: папки и этапы уже посчитаны там
    (не дублируем classify), здесь добавляем эмбеддинг документа, привязку к
    подэтапам (Вариант 1) и ключевые слова — и сохраняем строку.
    """
    from config import get_embedding
    doc_vec = get_embedding(summary or filename)
    subs = assign_substages(doc_vec, _substage_vectors(stage_ids or None))
    return upsert({
        "sha256": sha256, "filename": filename, "size_bytes": size_bytes, "mime": mime,
        "status": "indexed", "uploaded_at": uploaded_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "uploaded_by": uploaded_by, "summary": summary,
        "keywords": extract_keywords(summary), "embedding": doc_vec,
        "folders": folders or [], "stage_ids": stage_ids or [], "substages": subs,
    })


# ---------- Экран «этапы ↔ документы» ----------
def board() -> dict:
    """Данные для экрана: этапы/подэтапы и относящиеся к ним документы + без привязки."""
    import stages as stages_mod
    return build_board(stages_mod.list_stages(), list_docs())


# ---------- Разовое наполнение из старого registry.json ----------
def backfill_from_registry() -> int:
    """
    Переносит уже загруженные документы из файлового registry.json в таблицу documents
    (не трогая сам registry и пайплайн). Для каждого — считает подэтапы и вектор.
    Возвращает число перенесённых. Нужны запущенные Postgres и Ollama (эмбеддинги).
    """
    import json
    from config import REGISTRY_PATH, get_embedding
    if not REGISTRY_PATH.exists():
        return 0
    reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    count = 0
    for filename, e in reg.items():
        summary = e.get("summary") or ""
        try:
            doc_vec = get_embedding(summary or filename)
            subs = assign_substages(doc_vec, _substage_vectors(e.get("stage_ids") or None))
        except Exception:
            doc_vec, subs = None, []
        upsert({
            "sha256": e.get("sha256") or e.get("hash") or filename,   # ключ: хэш, иначе имя
            "filename": filename, "mime": e.get("mime"),
            "status": e.get("status", "indexed"), "uploaded_at": e.get("uploaded_at"),
            "summary": summary, "keywords": extract_keywords(summary), "embedding": doc_vec,
            "folders": e.get("folders") or [], "stage_ids": e.get("stage_ids") or [],
            "substages": subs,
        })
        count += 1
    return count
