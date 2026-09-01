"""
folders.py — смысловые папки как ЛОГИЧЕСКИЕ категории базы знаний.

Главный принцип (ТЗ): структуру знаний задаёт человек. Искусственный интеллект
НЕ создаёт папки сам — он лишь анализирует документы и относит их (и их части)
к уже существующим папкам. Папка не хранит физических копий документов: это
метка-категория, по которой чанки попадают в выборку при поиске. Один документ
или один чанк может относиться сразу к нескольким папкам.

Удаление папки убирает только логическую категорию и её связи — сами документы
остаются в системе и доступны через общую базу знаний «Все документы».

Хранилище — PostgreSQL (таблица folders, см. db.SCHEMA_STATEMENTS). Поля criteria
и stage_ids — JSONB-массивы строк:
  - criteria  — смысловые критерии отнесения информации к папке (не просто набор
                ключевых слов; ИИ понимает их смысл при классификации, фаза 2);
  - stage_ids — id связанных этапов обучения (из каталога planner).
"""

import re
import time
import uuid
from typing import Optional

from psycopg.types.json import Json

import db


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# Транслит для человекочитаемого slug папки (то же отображение, что в topics).
_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def slugify(text: str) -> str:
    text = (text or "").lower().strip().translate(_TRANSLIT)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or f"folder_{uuid.uuid4().hex[:6]}"


def _clean_list(values) -> list:
    """Список строк без пустых и дублей, порядок сохраняется."""
    out, seen = [], set()
    for v in values or []:
        if v is None:
            continue
        s = str(v).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _row(r: dict) -> dict:
    # JSONB psycopg3 отдаёт уже готовыми python-списками.
    return {
        "id": r["id"],
        "slug": r["slug"],
        "name": r["name"],
        "description": r.get("description") or "",
        "criteria": r.get("criteria") or [],
        "stage_ids": r.get("stage_ids") or [],
        "enabled": bool(r.get("enabled", True)),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def list_folders(include_disabled: bool = True) -> list:
    sql = "SELECT * FROM folders"
    if not include_disabled:
        sql += " WHERE enabled = TRUE"
    sql += " ORDER BY name"
    return [_row(r) for r in db.query(sql)]


def get_folder(folder_id: str) -> Optional[dict]:
    r = db.query("SELECT * FROM folders WHERE id = %s", (folder_id,), fetch="one")
    return _row(r) if r else None


def get_by_slug(slug: str) -> Optional[dict]:
    r = db.query("SELECT * FROM folders WHERE slug = %s", (slug,), fetch="one")
    return _row(r) if r else None


def _unique_slug(base: str) -> str:
    slug, i = base, 2
    while db.query("SELECT 1 FROM folders WHERE slug = %s", (slug,), fetch="one"):
        slug = f"{base}_{i}"
        i += 1
    return slug


def create_folder(name: str, description: str = "",
                  criteria: Optional[list] = None, stage_ids: Optional[list] = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Название папки обязательно")
    fid = uuid.uuid4().hex
    slug = _unique_slug(slugify(name))
    now = _now()
    db.execute(
        "INSERT INTO folders (id, slug, name, description, criteria, stage_ids, enabled, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s)",
        (fid, slug, name, description or "", Json(_clean_list(criteria)), Json(_clean_list(stage_ids)), now, now),
    )
    return get_folder(fid)


def update_folder(folder_id: str, **fields) -> Optional[dict]:
    """Частичное обновление: name / description / criteria / stage_ids / enabled."""
    allowed = {"name", "description", "criteria", "stage_ids", "enabled"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "name":
            value = str(value).strip()
            if not value:
                raise ValueError("Название папки обязательно")
        elif key in ("criteria", "stage_ids"):
            value = Json(_clean_list(value))
        elif key == "enabled":
            value = bool(value)
        sets.append(f"{key} = %s")
        params.append(value)
    if not sets:
        return get_folder(folder_id)
    sets.append("updated_at = %s")
    params.append(_now())
    params.append(folder_id)
    db.execute(f"UPDATE folders SET {', '.join(sets)} WHERE id = %s", tuple(params))
    return get_folder(folder_id)


def delete_folder(folder_id: str) -> bool:
    """Удаляет только логическую категорию. Документы и их чанки не трогаются
    (в фазе 2 здесь же будет сниматься метка папки с чанков в Qdrant)."""
    existed = get_folder(folder_id) is not None
    db.execute("DELETE FROM folders WHERE id = %s", (folder_id,))
    return existed


def seed_if_empty(items: list) -> int:
    """Заводит стартовые папки из сида, только если таблица пуста. Идемпотентно.
    Стартовая структура — из data/knowledge_seed.json (получена из исходного Excel);
    дальше человек управляет папками сам."""
    if db.query("SELECT 1 FROM folders LIMIT 1", fetch="one"):
        return 0
    n = 0
    for f in items or []:
        create_folder(f.get("name", ""), f.get("description", ""),
                      f.get("criteria"), f.get("stage_ids"))
        n += 1
    return n


if __name__ == "__main__":
    # Самопроверка чистой логики (без БД).
    assert slugify("Охрана труда") == "ohrana_truda"
    assert slugify("  ") .startswith("folder_")
    assert _clean_list([" a ", "a", "", "b", None]) == ["a", "b"]
    print("folders: slug/clean — OK")
