"""
Конструктор плана адаптации: этапы -> подэтапы -> расписание -> автоответы.

Что здесь происходит:

    каталог этапов (data/stage_catalog.json)
        -> человек в конструкторе собирает план: этапы, их длительность,
           подэтапы с описанием и временем срабатывания
        -> plan.json  — канонический машинный формат (источник истины)
        -> plan.md    — тот же план в удобном для LLM виде
        -> генерация: по каждому подэтапу LLM выбирает смысловые папки базы
           знаний, ищет в них чанки и пишет готовое сообщение сотруднику
        -> schedule.json — плоский список сообщений с временем отправки
           (формат под мессенджеры и самописное приложение)
        -> schedule.md  — человекочитаемая таблица этап / подэтап / дата и время / ответ

Время хранится ОТНОСИТЕЛЬНО даты выхода сотрудника: этап знает свою длительность
и якорь (до выхода / от даты выхода), подэтап — номер дня внутри этапа и время.
Абсолютные даты считаются подстановкой start_date, поэтому один и тот же план
переиспользуется для любого новичка.
"""

import json
import math
import time
import uuid
import threading
import unicodedata
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CATALOG_PATH = DATA_DIR / "stage_catalog.json"
PLANS_DIR = DATA_DIR / "plans"
PLANS_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA_VERSION = "1.0"
DEFAULT_TIMEZONE = "Europe/Moscow"

# Сколько чанков базы знаний уходит в контекст генерации одного подэтапа
CONTEXT_CHUNKS = 6

UNIT_DAYS = {"hours": 0, "days": 1, "weeks": 7, "months": 30}
KIND_IDS = {"message", "checklist", "survey", "quiz", "reminder", "system_check", "handover"}

_plans_lock = threading.Lock()


# ---------- Каталог этапов ----------
def load_catalog() -> dict:
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def catalog_stage(catalog_id: str) -> Optional[dict]:
    for stage in load_catalog()["stages"]:
        if stage["id"] == catalog_id:
            return stage
    return None


def catalog_stage_query(stage: dict) -> str:
    """Смысловой «запрос этапа» — заголовок + описание + брифы и теги всех его подэтапов.
    По нему чанки раскладываются по этапам (материализация «папок этапов»)."""
    parts = [stage.get("title", ""), stage.get("description", "")]
    for tpl in stage.get("substage_templates") or []:
        parts += [tpl.get("title", ""), tpl.get("brief", "")]
        parts += tpl.get("tags") or []
    return ". ".join(p for p in parts if p)


def catalog_substage_query(stage: dict, sub: dict) -> str:
    """Смысловой «запрос подэтапа» — заголовок этапа для контекста + заголовок, бриф и теги
    подэтапа. По нему чанки раскладываются по ПОДЭТАПАМ (тоньше этапа, мульти-лейбл)."""
    parts = [stage.get("title", ""), sub.get("title", ""), sub.get("brief", "")]
    parts += sub.get("tags") or []
    return ". ".join(p for p in parts if p)


def build_full_template(title: str = "Универсальный план адаптации") -> dict:
    """Полный универсальный шаблон плана из ВСЕГО каталога: все этапы и все подэтапы с их
    описаниями, длительностями и временем. Профессионально-независимый — дальше человек
    редактирует под задачу. Возвращает нормализованный план (без сохранения)."""
    cat = load_catalog()
    raw = {
        "title": title,
        "role": "",
        "description": "Полный универсальный шаблон адаптации: все этапы и подэтапы каталога. "
                       "Единый для всех профессий — специфику даёт база знаний при генерации. Редактируйте под задачу.",
        "stages": [],
    }
    for st in cat.get("stages") or []:
        raw_stage = {
            "catalog_id": st["id"],
            "title": st.get("title", ""),
            "description": st.get("description", ""),
            "anchor": st.get("anchor", "from_start"),
            "duration": st.get("default_duration") or {"value": 1, "unit": "days"},
            "substages": [],
        }
        for tpl in st.get("substage_templates") or []:
            raw_stage["substages"].append({
                "catalog_id": tpl["id"],
                "title": tpl.get("title", ""),
                "kind": tpl.get("kind", "message"),
                "brief": tpl.get("brief", ""),
                "tags": tpl.get("tags") or [],
                "source": "template",
                "schedule": {"day": 1, "time": tpl.get("default_time", "09:00")},
            })
        raw["stages"].append(raw_stage)
    return normalize_plan(raw)


# ---------- Длительности и расчёт дат ----------
def stage_span_days(duration: dict) -> int:
    """
    Сколько календарных дней занимает этап.
    Для единицы «часы» дней выбора нет — этап укладывается в минимально
    необходимое число суток (12 ч -> 1 день, 36 ч -> 2 дня).
    """
    value = max(1, int(duration.get("value") or 1))
    unit = duration.get("unit") or "days"
    if unit == "hours":
        return max(1, math.ceil(value / 24))
    return max(1, value * UNIT_DAYS.get(unit, 1))


def stage_day_choice(duration: dict) -> bool:
    """Можно ли выбирать день внутри этапа (для «часов» — только время)."""
    return (duration.get("unit") or "days") != "hours"


def compute_offsets(plan: dict) -> dict:
    """
    Возвращает {stage_id: смещение первого дня этапа в днях от даты выхода}.

    Этапы с якорем before_start выстраиваются подряд так, чтобы последний
    их день приходился на день перед выходом (-1). Этапы from_start идут
    подряд начиная с самого дня выхода (0).
    """
    stages = plan.get("stages") or []
    before = [s for s in stages if s.get("anchor") == "before_start"]
    after = [s for s in stages if s.get("anchor") != "before_start"]

    offsets = {}

    cursor = -sum(stage_span_days(s.get("duration", {})) for s in before)
    for stage in before:
        offsets[stage["id"]] = cursor
        cursor += stage_span_days(stage.get("duration", {}))

    cursor = 0
    for stage in after:
        offsets[stage["id"]] = cursor
        cursor += stage_span_days(stage.get("duration", {}))

    return offsets


def parse_start_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_schedule(plan: dict) -> list:
    """
    Разворачивает план в плоский хронологический список позиций расписания.
    Каждая позиция уже знает своё смещение в днях и, если задана дата выхода,
    абсолютные дату и время отправки.
    """
    offsets = compute_offsets(plan)
    start = parse_start_date(plan.get("start_date"))
    items = []

    for stage_index, stage in enumerate(plan.get("stages") or [], start=1):
        stage_offset = offsets.get(stage["id"], 0)
        day_choice = stage_day_choice(stage.get("duration", {}))
        span = stage_span_days(stage.get("duration", {}))

        for sub_index, sub in enumerate(stage.get("substages") or [], start=1):
            schedule = sub.get("schedule") or {}
            day = 1 if not day_choice else max(1, min(span, int(schedule.get("day") or 1)))
            send_time = schedule.get("time") or "09:00"
            offset_days = stage_offset + (day - 1)

            send_at = None
            if start:
                hh, mm = _parse_time(send_time)
                send_at = datetime.combine(start + timedelta(days=offset_days),
                                           datetime.min.time()).replace(hour=hh, minute=mm)

            items.append({
                "message_id": f"{stage['id']}.{sub['id']}",
                "stage": {
                    "id": stage["id"],
                    "catalog_id": stage.get("catalog_id"),
                    "order": stage_index,
                    "title": stage.get("title", ""),
                },
                "substage": {
                    "id": sub["id"],
                    "catalog_id": sub.get("catalog_id"),
                    "order": sub_index,
                    "title": sub.get("title", ""),
                    "kind": sub.get("kind", "message"),
                    "brief": sub.get("brief", ""),
                },
                "schedule": {
                    "anchor": stage.get("anchor", "from_start"),
                    "stage_day": day,
                    "time": send_time,
                    "offset_days": offset_days,
                    "send_at": send_at.isoformat(timespec="minutes") if send_at else None,
                },
            })

    items.sort(key=lambda i: (i["schedule"]["offset_days"], i["schedule"]["time"],
                             i["stage"]["order"], i["substage"]["order"]))
    return items


def _parse_time(value: str) -> tuple:
    try:
        hh, mm = str(value).split(":")[:2]
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except (ValueError, AttributeError):
        return 9, 0


# ---------- Нормализация плана из конструктора ----------
def _slug(value: str, fallback: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    out = [c if c.isalnum() else "_" for c in text]
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug[:40] or fallback


def normalize_plan(raw: dict, plan_id: Optional[str] = None) -> dict:
    """Приводит присланный фронтендом план к каноническому виду и чинит очевидное."""
    now = datetime.now().isoformat(timespec="seconds")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id or raw.get("plan_id") or str(uuid.uuid4()),
        "title": (raw.get("title") or "План адаптации").strip(),
        "role": (raw.get("role") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "start_date": str(raw.get("start_date") or "")[:10] or None,
        "timezone": raw.get("timezone") or DEFAULT_TIMEZONE,
        "created_at": raw.get("created_at") or now,
        "updated_at": now,
        "stages": [],
    }

    used_stage_ids = set()
    for s_index, raw_stage in enumerate(raw.get("stages") or [], start=1):
        stage_id = raw_stage.get("id") or f"st{s_index}_{_slug(raw_stage.get('catalog_id') or raw_stage.get('title'), f'stage{s_index}')}"
        while stage_id in used_stage_ids:
            stage_id = f"{stage_id}_{s_index}"
        used_stage_ids.add(stage_id)

        duration = raw_stage.get("duration") or {}
        unit = duration.get("unit") if duration.get("unit") in UNIT_DAYS else "days"
        try:
            value = max(1, int(duration.get("value") or 1))
        except (TypeError, ValueError):
            value = 1

        stage = {
            "id": stage_id,
            "catalog_id": raw_stage.get("catalog_id"),
            "order": s_index,
            "title": (raw_stage.get("title") or "").strip() or f"Этап {s_index}",
            "description": (raw_stage.get("description") or "").strip(),
            "anchor": "before_start" if raw_stage.get("anchor") == "before_start" else "from_start",
            "duration": {"value": value, "unit": unit},
            "substages": [],
        }

        span = stage_span_days(stage["duration"])
        day_choice = stage_day_choice(stage["duration"])

        used_sub_ids = set()
        for sub_index, raw_sub in enumerate(raw_stage.get("substages") or [], start=1):
            sub_id = raw_sub.get("id") or f"s{sub_index}_{_slug(raw_sub.get('catalog_id') or raw_sub.get('title'), f'sub{sub_index}')}"
            while sub_id in used_sub_ids:
                sub_id = f"{sub_id}_{sub_index}"
            used_sub_ids.add(sub_id)

            raw_schedule = raw_sub.get("schedule") or {}
            try:
                day = int(raw_schedule.get("day") or 1)
            except (TypeError, ValueError):
                day = 1
            day = 1 if not day_choice else max(1, min(span, day))
            hh, mm = _parse_time(raw_schedule.get("time") or "09:00")

            kind = raw_sub.get("kind") if raw_sub.get("kind") in KIND_IDS else "message"
            stage["substages"].append({
                "id": sub_id,
                "catalog_id": raw_sub.get("catalog_id"),
                "order": sub_index,
                "title": (raw_sub.get("title") or "").strip() or f"Подэтап {sub_index}",
                "kind": kind,
                "brief": (raw_sub.get("brief") or "").strip(),
                "source": "manual" if raw_sub.get("source") == "manual" else "template",
                "tags": [t for t in (raw_sub.get("tags") or []) if isinstance(t, str)],
                "schedule": {"day": day, "time": f"{hh:02d}:{mm:02d}"},
            })

        plan["stages"].append(stage)

    return plan


# ---------- Хранилище планов ----------
def plan_dir(plan_id: str) -> Path:
    safe = "".join(c for c in str(plan_id or "") if c.isalnum() or c in "-_")
    # Пустой или состоящий только из «-_» id схлопывается в сам PLANS_DIR — тогда
    # delete_plan снёс бы ВСЕ планы разом (rglob+rmdir по корню). Отвергаем такие id.
    if not safe.strip("-_"):
        raise ValueError(f"Некорректный идентификатор плана: {plan_id!r}")
    resolved = (PLANS_DIR / safe).resolve()
    if resolved.parent != PLANS_DIR.resolve():
        raise ValueError(f"Некорректный идентификатор плана: {plan_id!r}")
    return resolved


def save_plan(plan: dict) -> dict:
    directory = plan_dir(plan["plan_id"])
    directory.mkdir(parents=True, exist_ok=True)
    with _plans_lock:
        _write_json(directory / "plan.json", plan)
        (directory / "plan.md").write_text(render_plan_md(plan), encoding="utf-8")
    return plan


def load_plan(plan_id: str) -> Optional[dict]:
    try:
        path = plan_dir(plan_id) / "plan.json"
    except ValueError:
        return None
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def duplicate_plan(plan_id: str, new_title: Optional[str] = None) -> Optional[dict]:
    """
    Копия плана под смежную должность: та же структура этапов/подэтапов, но новый
    plan_id и заголовок. Сгенерированные тексты и расписание НЕ копируются — их
    перегенерируют под новую должность (иначе в копии остались бы ответы про старую).
    """
    src = load_plan(plan_id)
    if src is None:
        return None
    src = dict(src)
    src.pop("plan_id", None)     # normalize_plan выдаст свежий id
    src.pop("created_at", None)  # и свежие даты
    src["title"] = (new_title or "").strip() or f"{src.get('title', 'План адаптации')} (копия)"
    return save_plan(normalize_plan(src))


def list_plans() -> list:
    plans = []
    for directory in PLANS_DIR.iterdir():
        if not directory.is_dir():
            continue
        plan_path = directory / "plan.json"
        if not plan_path.exists():
            continue
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        plans.append({
            "plan_id": plan.get("plan_id"),
            "title": plan.get("title"),
            "role": plan.get("role"),
            "start_date": plan.get("start_date"),
            "stages": len(plan.get("stages") or []),
            "substages": sum(len(s.get("substages") or []) for s in plan.get("stages") or []),
            "updated_at": plan.get("updated_at"),
            "generated": (directory / "schedule.json").exists(),
        })
    return sorted(plans, key=lambda p: p.get("updated_at") or "", reverse=True)


def delete_plan(plan_id: str) -> bool:
    try:
        directory = plan_dir(plan_id)
    except ValueError:
        return False
    if not directory.exists():
        return False
    for item in sorted(directory.rglob("*"), reverse=True):
        item.unlink() if item.is_file() else item.rmdir()
    directory.rmdir()
    return True


def profession_slug(position: str) -> str:
    """Короткий стабильный slug должности для имени файла расписания профессии."""
    return _slug(position, "obshiy")


def _schedule_path(plan_id: str, profession: str = ""):
    """schedule.json — общее расписание; schedules/<slug>.json — под конкретную должность."""
    directory = plan_dir(plan_id)
    if (profession or "").strip():
        return directory / "schedules" / f"{profession_slug(profession)}.json"
    return directory / "schedule.json"


def load_schedule(plan_id: str, profession: str = "") -> Optional[dict]:
    """Расписание плана. При указанной должности берём расписание её профессии; если его нет —
    откат на общее (schedule.json). Так план один, а контент — под профессию из аккаунта."""
    try:
        path = _schedule_path(plan_id, profession)
    except ValueError:
        return None
    if not path.exists() and profession:
        try:
            path = _schedule_path(plan_id, "")   # откат на общее
        except ValueError:
            return None
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_schedule_professions(plan_id: str) -> list:
    """Список профессий, под которые сгенерированы расписания (по файлам schedules/*.json)."""
    try:
        directory = plan_dir(plan_id) / "schedules"
    except ValueError:
        return []
    if not directory.exists():
        return []
    out = []
    for f in sorted(directory.glob("*.json")):
        try:
            out.append({"slug": f.stem, "profession": json.loads(f.read_text(encoding="utf-8")).get("profession", "")})
        except (json.JSONDecodeError, OSError):
            continue
    return out


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------- Рендер плана в Markdown (формат для LLM) ----------
def _duration_label(duration: dict) -> str:
    titles = {"hours": ("час", "часа", "часов"), "days": ("день", "дня", "дней"),
              "weeks": ("неделя", "недели", "недель"), "months": ("месяц", "месяца", "месяцев")}
    value = duration.get("value", 1)
    forms = titles.get(duration.get("unit", "days"), titles["days"])
    if value % 10 == 1 and value % 100 != 11:
        word = forms[0]
    elif value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        word = forms[1]
    else:
        word = forms[2]
    return f"{value} {word}"


def render_plan_md(plan: dict) -> str:
    """План в виде, удобном для чтения LLM: заголовки, метаданные, подэтапы списком."""
    offsets = compute_offsets(plan)
    lines = [
        f"# {plan.get('title', 'План адаптации')}",
        "",
        f"- **Идентификатор плана:** `{plan.get('plan_id')}`",
        f"- **Должность / роль:** {plan.get('role') or 'не указана'}",
        f"- **Дата выхода сотрудника:** {plan.get('start_date') or 'не задана (план относительный)'}",
        f"- **Часовой пояс:** {plan.get('timezone')}",
        f"- **Этапов:** {len(plan.get('stages') or [])}",
        f"- **Версия схемы:** {plan.get('schema_version')}",
    ]
    if plan.get("description"):
        lines += ["", plan["description"]]

    lines += ["", "Время подэтапов задано относительно даты выхода сотрудника: "
                  "этап имеет якорь (до выхода / от даты выхода) и длительность, "
                  "подэтап — номер дня внутри этапа и время суток.", ""]

    for stage in plan.get("stages") or []:
        offset = offsets.get(stage["id"], 0)
        anchor = "до выхода на работу" if stage.get("anchor") == "before_start" else "от даты выхода"
        span = stage_span_days(stage.get("duration", {}))
        lines += [
            f"## Этап {stage.get('order')}. {stage.get('title')}",
            "",
            f"- **id:** `{stage['id']}`",
            f"- **Длительность:** {_duration_label(stage.get('duration', {}))} ({span} кал. дн.)",
            f"- **Якорь:** {anchor}",
            f"- **Смещение первого дня этапа:** {offset:+d} дн. от даты выхода",
        ]
        if stage.get("description"):
            lines += [f"- **Описание:** {stage['description']}"]
        lines += [""]

        if not stage.get("substages"):
            lines += ["_Подэтапы не заданы._", ""]
            continue

        for sub in stage["substages"]:
            schedule = sub.get("schedule") or {}
            when = f"день {schedule.get('day', 1)}, {schedule.get('time', '09:00')}"
            if not stage_day_choice(stage.get("duration", {})):
                when = f"{schedule.get('time', '09:00')} (этап задан в часах — день не выбирается)"
            lines += [
                f"### Подэтап {sub.get('order')}. {sub.get('title')}",
                "",
                f"- **id:** `{sub['id']}`",
                f"- **Тип:** {sub.get('kind')}",
                f"- **Когда:** {when}",
                f"- **Смещение от даты выхода:** {offset + int(schedule.get('day', 1)) - 1:+d} дн.",
                f"- **Что должен написать бот:** {sub.get('brief') or '—'}",
            ]
            if sub.get("tags"):
                lines += [f"- **Темы для поиска в базе знаний:** {', '.join(sub['tags'])}"]
            lines += [""]

    return "\n".join(lines)


# ---------- Рендер результата генерации ----------
def render_schedule_md(schedule: dict) -> str:
    plan_title = schedule.get("plan_title", "План адаптации")
    employee = schedule.get("employee") or {}
    header = f"# Расписание адаптации — {employee['full_name']}" if employee.get("full_name") \
        else f"# Расписание автоответов — {plan_title}"

    lines = [header, ""]
    if employee:
        lines += [
            f"- **Сотрудник:** {employee.get('full_name')}",
            f"- **Должность:** {employee.get('position') or 'не указана'}",
            f"- **Подразделение:** {employee.get('department') or 'не указано'}",
            f"- **Наставник:** {employee.get('mentor') or 'не назначен'}",
            f"- **Руководитель:** {employee.get('manager') or 'не указан'}",
            f"- **План-шаблон:** {plan_title}",
        ]
    lines += [
        f"- **Идентификатор плана:** `{schedule.get('plan_id')}`",
        f"- **Должность / роль плана:** {schedule.get('role') or 'не указана'}",
        f"- **Дата выхода сотрудника:** {schedule.get('start_date') or 'не задана (даты относительные)'}",
        f"- **Часовой пояс:** {schedule.get('timezone')}",
        f"- **Сообщений:** {len(schedule.get('messages') or [])}",
        f"- **Сгенерировано:** {schedule.get('generated_at')}",
        "",
        "## Сводка",
        "",
        "| Этап | Подэтап | Дата и время отправки | Тип |",
        "|---|---|---|---|",
    ]
    for msg in schedule.get("messages") or []:
        lines.append(
            f"| {msg['stage']['order']}. {msg['stage']['title']} "
            f"| {msg['substage']['order']}. {msg['substage']['title']} "
            f"| {_when_label(msg)} | {msg['substage']['kind']} |"
        )

    lines += ["", "## Сообщения", ""]
    for msg in schedule.get("messages") or []:
        lines += [
            f"### {msg['stage']['title']} → {msg['substage']['title']}",
            "",
            f"- **id сообщения:** `{msg['message_id']}`",
            f"- **Дата и время отправки:** {_when_label(msg)}",
            f"- **Тип:** {msg['substage']['kind']}",
            f"- **Статус:** {msg.get('status')}",
        ]
        if msg.get("topics_used"):
            lines.append(f"- **Темы базы знаний:** {', '.join(msg['topics_used'])}")
        if msg.get("sources"):
            srcs = ", ".join(sorted({s.get("source") or "?" for s in msg["sources"]}))
            lines.append(f"- **Документы-источники:** {srcs}")
        if msg.get("error"):
            lines.append(f"- **Ошибка:** {msg['error']}")
        lines += ["", "**Ответ:**", "", (msg.get("content", {}).get("text") or "—"), ""]

    return "\n".join(lines)


def _when_label(msg: dict) -> str:
    schedule = msg.get("schedule") or {}
    if schedule.get("send_at"):
        return schedule["send_at"].replace("T", " ")
    anchor = "до выхода" if schedule.get("anchor") == "before_start" else "от выхода"
    return f"{schedule.get('offset_days'):+d} дн. ({anchor}), {schedule.get('time')}"


# ---------- Генерация автоответов ----------
PICK_TOPICS_SYSTEM = """
Ты — навигатор по корпоративной базе знаний. Тебе дают описание подэтапа программы
адаптации сотрудника и список тем (папок) с документами.

Задача: выбрать темы, в которых лежат документы, нужные чтобы написать этот подэтап.
Выбирай от 1 до 3 тем, самые релевантные. Если ни одна тема явно не подходит,
выбери одну наиболее близкую.

Верни ТОЛЬКО JSON со slug'ами тем: {"topics": ["slug-temy", "drugoy-slug"]}
"""

KIND_INSTRUCTIONS = {
    "message": "Напиши связное сообщение сотруднику: 3–7 предложений, тёплый деловой тон, конкретика из документов.",
    "checklist": "Оформи как чек-лист: короткое вступление и пронумерованные пункты, каждый — проверяемое действие.",
    "survey": "Оформи как опрос: короткое вступление и вопросы с вариантами ответа. Укажи, что делать при тревожных ответах.",
    "quiz": "Оформи как мини-тест: один-три вопроса, к каждому 3 варианта ответа и пометка, какой верный.",
    "reminder": "Напиши короткое push-напоминание: 1–3 предложения, только суть и действие.",
    "system_check": "Оформи как перечень проверяемых условий с пороговыми значениями и указанием, что происходит при невыполнении.",
    "handover": "Оформи как задачу ответственному человеку (наставнику, руководителю или HR): что сделать, к какому сроку, что зафиксировать.",
}

GENERATE_SYSTEM = """
Ты — НейроМастер, виртуальный наставник нового сотрудника производственной компании.
Ты пишешь готовое сообщение, которое система отправит сотруднику в назначенное время.

Правила:
- Опирайся ТОЛЬКО на предоставленные фрагменты внутренних документов компании.
- Не выдумывай цифры, сроки, нормы и названия. Если данных в контексте нет —
  напиши общую формулировку и пометь в конце строкой: «Уточнить у HR: <что именно>».
- Обращайся к сотруднику на «вы», имя подставляй плейсхолдером [Имя].
  Другие неизвестные данные — плейсхолдерами вида [ФИО наставника], [номер КПП].
- Пиши по-русски, без вступлений вроде «Конечно» и без рассуждений.
- Не пиши заголовки, дату и время — только сам текст сообщения.
"""


def _substage_query(stage: dict, substage: dict) -> str:
    parts = [stage.get("title", ""), substage.get("title", ""), substage.get("brief", "")]
    parts += substage.get("tags") or []
    return ". ".join(p for p in parts if p)


def pick_topics(stage: dict, substage: dict, topic_list: list) -> list:
    """
    LLM выбирает смысловые папки базы знаний под подэтап. Возвращает slug'и папок.
    Фолбэк — все включённые папки (поиск без сужения). topic_list — список папок
    (folders.list_folders): slug, name, description, criteria.
    """
    from rag import small_llm, parse_json_response

    available = list(topic_list or [])
    if not available:
        return []
    slugs = {t["slug"].casefold(): t["slug"] for t in available}

    topic_lines = "\n".join(
        f"- {t['slug']} — {t.get('name', '')}: {t.get('description', '')} "
        f"[{'; '.join(t.get('criteria') or [])}]"
        for t in available
    )
    user = (
        f"Темы базы знаний:\n{topic_lines}\n\n"
        f"Этап: {stage.get('title')}\n"
        f"Подэтап: {substage.get('title')} (тип: {substage.get('kind')})\n"
        f"Что должен написать бот: {substage.get('brief')}\n"
        f"Ключевые слова подэтапа: {', '.join(substage.get('tags') or []) or '—'}"
    )

    try:
        data = parse_json_response(small_llm(PICK_TOPICS_SYSTEM, user, step_name="PICK_TOPICS"))
        picked = [slugs[str(s).casefold()] for s in (data.get("topics") or [])
                  if str(s).casefold() in slugs]
    except Exception:
        picked = []

    return picked or [t["slug"] for t in available]


def generate_substage_message(stage: dict, substage: dict, topic_list: list, position: str = "") -> dict:
    """Генерирует текст одного подэтапа. Ошибки не поднимает — возвращает status=error.
    position — должность/профессия сотрудника (из аккаунта): чанки берутся из «папки подэтапа»
    (разложенные по этому подэтапу), приоритет — чанкам этой профессии (буст по должности)."""
    import indexing
    from rag import big_llm

    result = {"topics_used": [], "sources": [], "status": "generated", "error": None,
              "content": {"format": "markdown", "text": ""}}
    try:
        picked = pick_topics(stage, substage, topic_list)
        result["topics_used"] = picked

        chunks = indexing.search_chunks(_substage_query(stage, substage), picked, limit=CONTEXT_CHUNKS,
                                        plan_stage=stage.get("catalog_id"),
                                        plan_substage=substage.get("catalog_id"), position=position)
        result["sources"] = [{"source": c["source"], "folders": c.get("folders") or [],
                              "page": c["page"], "score": round(c["score"], 3)} for c in chunks]

        if not chunks:
            result["status"] = "error"
            result["error"] = "В базе знаний не нашлось подходящих фрагментов"
            return result

        context = "\n\n".join(
            f"--- Фрагмент {i + 1} (документ: {c['source']}) ---\n{c['text']}"
            for i, c in enumerate(chunks)
        )
        user = (
            f"Этап программы адаптации: {stage.get('title')}\n"
            f"Подэтап: {substage.get('title')}\n"
            f"Тип сообщения: {substage.get('kind')}. "
            f"{KIND_INSTRUCTIONS.get(substage.get('kind'), KIND_INSTRUCTIONS['message'])}\n\n"
            f"Что должен написать бот:\n{substage.get('brief') or substage.get('title')}\n\n"
            f"Фрагменты внутренних документов компании:\n{context}"
        )
        result["content"]["text"] = big_llm(GENERATE_SYSTEM, user).strip()
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    return result


def build_schedule(plan: dict, generated: dict, profession: str = "") -> dict:
    """
    Собирает финальный документ: плоский список сообщений с временем отправки.
    generated — {message_id: результат generate_substage_message}.
    profession — должность, под которую собран контент ("" = общее расписание).
    """
    items = resolve_schedule(plan)
    messages = []
    for item in items:
        payload = generated.get(item["message_id"], {})
        messages.append({
            **item,
            "content": payload.get("content", {"format": "markdown", "text": ""}),
            "topics_used": payload.get("topics_used", []),
            "sources": payload.get("sources", []),
            "status": payload.get("status", "pending"),
            "error": payload.get("error"),
            # Зарезервировано под мессенджеры: кнопки/варианты ответа сотрудника
            "actions": payload.get("actions", []),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan.get("plan_id"),
        "plan_title": plan.get("title"),
        "role": plan.get("role"),
        "profession": profession,
        "start_date": plan.get("start_date"),
        "timezone": plan.get("timezone", DEFAULT_TIMEZONE),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }


def save_schedule(plan_id: str, schedule: dict, profession: str = ""):
    """Сохраняет расписание. Пустая profession -> общее (schedule.json); иначе — под должность
    (schedules/<slug>.json). Так один план хранит и общий контент, и вариант под профессию."""
    path = _schedule_path(plan_id, profession)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _plans_lock:
        _write_json(path, schedule)
        path.with_suffix(".md").write_text(render_schedule_md(schedule), encoding="utf-8")


# ---------- Фоновые задачи генерации ----------
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **fields):
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {"job_id": job_id})
        job.update(fields)
        return dict(job)


def get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def cancel_job(job_id: str) -> Optional[dict]:
    """Помечает задачу генерации на отмену. Цикл run() увидит флаг перед следующим
    подэтапом, сохранит уже сгенерированное и завершится статусом 'cancelled'."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        if job.get("status") in ("queued", "running"):
            job["cancel"] = True
        return dict(job)


def start_generation(plan: dict, positions: Optional[list] = None) -> dict:
    """Запускает генерацию плана в фоне. positions — список уникальных должностей (из штатки):
    под КАЖДУЮ генерируется своё расписание (чанки её профессии + общие), сотрудники этой
    должности берут готовое. Без positions — одно общее расписание (profession="")."""
    import folders

    # Уникальные должности + общее ("") как фолбэк для профессий без своего расписания.
    profs = []
    for p in (positions or []):
        p = (p or "").strip()
        if p and p not in profs:
            profs.append(p)
    profs = profs or [""]                 # хотя бы общее расписание
    if profs != [""] and "" not in profs:
        profs = profs + [""]              # плюс общее — для не перечисленных должностей

    job_id = str(uuid.uuid4())
    items = resolve_schedule(plan)
    _set_job(job_id, plan_id=plan["plan_id"], status="queued", total=len(items) * len(profs), done=0,
             current=None, started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), finished_at=None,
             errors=0, error=None, professions=len(profs))

    def run():
        _set_job(job_id, status="running")
        errors = 0
        done = 0
        try:
            topic_list = folders.list_folders(include_disabled=False)
            stages_by_id = {s["id"]: s for s in plan.get("stages") or []}
            for prof in profs:
                generated = {}
                label = prof or "общее"
                cancelled = False
                for item in items:
                    if (get_job(job_id) or {}).get("cancel"):   # запрошена отмена — стоп
                        cancelled = True
                        break
                    stage = stages_by_id.get(item["stage"]["id"], {})
                    substage = next(
                        (s for s in stage.get("substages", []) if s["id"] == item["substage"]["id"]),
                        item["substage"],
                    )
                    _set_job(job_id, current=f"[{label}] {item['stage']['title']} → {item['substage']['title']}",
                             done=done)
                    payload = generate_substage_message(stage, substage, topic_list, position=prof)
                    if payload["status"] == "error":
                        errors += 1
                    generated[item["message_id"]] = payload
                    done += 1
                    _set_job(job_id, done=done, errors=errors)
                if generated:   # сохраняем, что успели (частичное расписание не теряем)
                    save_schedule(plan["plan_id"], build_schedule(plan, generated, profession=prof), profession=prof)
                if cancelled:
                    _set_job(job_id, status="cancelled", current=None,
                             finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                    return
            _set_job(job_id, status="done", current=None,
                     finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception as e:
            _set_job(job_id, status="error", error=str(e),
                     finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    threading.Thread(target=run, name=f"generate-{job_id[:8]}", daemon=True).start()
    return get_job(job_id)


def regenerate_one(plan: dict, message_id: str, profession: str = "") -> Optional[dict]:
    """
    Перегенерирует один подэтап и обновляет сохранённое расписание нужной профессии.
    Возвращает обновлённое сообщение или None, если подэтап не найден.
    """
    import folders

    items = {i["message_id"]: i for i in resolve_schedule(plan)}
    item = items.get(message_id)
    if not item:
        return None

    stage = next((s for s in plan.get("stages") or [] if s["id"] == item["stage"]["id"]), {})
    substage = next((s for s in stage.get("substages", []) if s["id"] == item["substage"]["id"]), None)
    if substage is None:
        return None

    payload = generate_substage_message(stage, substage, folders.list_folders(include_disabled=False),
                                        position=profession)

    schedule = load_schedule(plan["plan_id"], profession)
    if schedule is None:
        schedule = build_schedule(plan, {message_id: payload}, profession=profession)
    else:
        for index, msg in enumerate(schedule.get("messages") or []):
            if msg.get("message_id") == message_id:
                schedule["messages"][index] = {**item, **payload,
                                               "actions": msg.get("actions", [])}
                break
        else:
            schedule.setdefault("messages", []).append({**item, **payload, "actions": []})
        schedule["generated_at"] = datetime.now().isoformat(timespec="seconds")

    save_schedule(plan["plan_id"], schedule, profession=profession)
    return next((m for m in schedule["messages"] if m["message_id"] == message_id), None)
