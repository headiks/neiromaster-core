"""
stages.py — этапы обучения (блоки) и подэтапы: «структура обучения» из ТЗ.

Этап определяет контекст, в котором находится сотрудник (см. §6 ТЗ — текущий этап
из прогресса обучения повышает приоритет релевантной информации при поиске), и
служит целью для связей смысловых папок (folders.stage_ids). Структурой управляет
человек: создать / переименовать / удалить этап, менять порядок, править подэтапы.

Хранилище — PostgreSQL (таблица stages). substages — JSONB-массив {id, title}.
Стартовая структура заводится из data/knowledge_seed.json при пустой таблице.
"""

import time
import uuid
from typing import Optional

from psycopg.types.json import Json

import db


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _subs(values) -> list:
    """Нормализуем подэтапы к [{id, title}] — сохраняем id, если он уже задан."""
    out = []
    for v in values or []:
        if v is None:
            continue
        if isinstance(v, dict):
            title = str(v.get("title", "")).strip()
            sid = str(v.get("id") or "").strip() or f"s{uuid.uuid4().hex[:8]}"
        else:
            title, sid = str(v).strip(), f"s{uuid.uuid4().hex[:8]}"
        if title:
            out.append({"id": sid, "title": title})
    return out


def _row(r: dict) -> dict:
    return {
        "id": r["id"],
        "title": r["title"],
        "description": r.get("description") or "",
        "substages": r.get("substages") or [],
        "position": r.get("position", 0),
    }


def list_stages() -> list:
    return [_row(r) for r in db.query("SELECT * FROM stages ORDER BY position, title")]


def get_stage(stage_id: str) -> Optional[dict]:
    r = db.query("SELECT * FROM stages WHERE id = %s", (stage_id,), fetch="one")
    return _row(r) if r else None


def _next_position() -> int:
    r = db.query("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM stages", fetch="one")
    return r["p"] if r else 1


def create_stage(title: str, description: str = "", substages: Optional[list] = None,
                 stage_id: Optional[str] = None) -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("Название этапа обязательно")
    sid = (stage_id or "").strip() or f"stg{uuid.uuid4().hex[:8]}"
    now = _now()
    db.execute(
        "INSERT INTO stages (id, title, description, substages, position, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (sid, title, description or "", Json(_subs(substages)), _next_position(), now, now),
    )
    return get_stage(sid)


def update_stage(stage_id: str, **fields) -> Optional[dict]:
    allowed = {"title", "description", "substages", "position"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "title":
            value = str(value).strip()
            if not value:
                raise ValueError("Название этапа обязательно")
        elif key == "substages":
            value = Json(_subs(value))
        sets.append(f"{key} = %s")
        params.append(value)
    if not sets:
        return get_stage(stage_id)
    sets.append("updated_at = %s")
    params.append(_now())
    params.append(stage_id)
    db.execute(f"UPDATE stages SET {', '.join(sets)} WHERE id = %s", tuple(params))
    return get_stage(stage_id)


def delete_stage(stage_id: str) -> bool:
    existed = get_stage(stage_id) is not None
    db.execute("DELETE FROM stages WHERE id = %s", (stage_id,))
    return existed


def seed_if_empty(items: list) -> int:
    """Заводит стартовые этапы из сида, только если таблица пуста. Идемпотентно."""
    if db.query("SELECT 1 FROM stages LIMIT 1", fetch="one"):
        return 0
    n = 0
    for pos, s in enumerate(items or [], start=1):
        now = _now()
        db.execute(
            "INSERT INTO stages (id, title, description, substages, position, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (s["id"], s["title"], s.get("description", ""), Json(_subs(s.get("substages"))), pos, now, now),
        )
        n += 1
    return n


if __name__ == "__main__":
    subs = _subs([{"id": "a", "title": " X "}, "Y", {"title": ""}])
    assert subs[0] == {"id": "a", "title": "X"}
    assert subs[1]["title"] == "Y" and subs[1]["id"].startswith("s")
    assert len(subs) == 2
    print("stages: subs — OK")
