"""
LLM-слой (Ollama, 14B): два прохода разметки со СТРОГИМ JSON через format-constraint
(JSON schema), temperature=0 и фиксированным seed — воспроизводимо. Валидацию/нормализацию
ответа делает core.coerce_section_labels (проход 2) и professions.match_to_staffing.
"""

import json
import requests

from config import OLLAMA_URL

MODEL = "qwen3:14b"
SEED = 7
TIMEOUT = 300
PROMPT_VERSION = "docpipe-1"

_HEAD_TOKENS = 3000   # сколько начала документа отдаём в проход 1 (≈ символов * 3)


def _chat(system: str, user: str, schema: dict) -> dict:
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False, "think": False,
        "format": schema,                                  # структурированный вывод (JSON schema)
        "options": {"temperature": 0, "seed": SEED},
    }, timeout=TIMEOUT)
    r.raise_for_status()
    content = r.json()["message"]["content"]
    return json.loads(content)


# ---------- Проход 1: карточка документа ----------
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string"},
        "summary": {"type": "string"},
        "audience": {"type": "array", "items": {"type": "string"}},
        "scope": {"type": "string", "enum": ["mono_profession", "multi_profession", "general"]},
    },
    "required": ["doc_type", "summary", "audience", "scope"],
}

CARD_SYSTEM = """Ты — аналитик корпоративной базы знаний. По заголовку, оглавлению и началу
документа составь его карточку. audience — список должностей, которым документ адресован
(или ["все"], если для всех). scope: mono_profession — документ про одну должность;
multi_profession — про несколько; general — общий для всех. Отвечай строго по схеме, по-русски."""


def doc_card(title: str, toc: str, head_text: str) -> dict:
    user = (f"Заголовок: {title}\n\nОглавление:\n{toc or '—'}\n\n"
            f"Начало документа:\n{(head_text or '')[:_HEAD_TOKENS * 3]}")
    return _chat(CARD_SYSTEM, user, CARD_SCHEMA)


# ---------- Проход 2: разметка секции ----------
SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_meaningful": {"type": "boolean"},
        "substages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["id", "confidence"],
            },
        },
        "professions": {"type": "array", "items": {"type": "string"}},
        "is_general": {"type": "boolean"},
        "why": {"type": "string"},
    },
    "required": ["is_meaningful", "substages", "professions", "is_general", "why"],
}

SECTION_SYSTEM = """Ты размечаешь ФРАГМЕНТ внутреннего документа относительно плана адаптации.
Тебе дают карточку документа, путь заголовков, текст фрагмента, ПОЛНЫЙ список подэтапов плана
(с id и описанием) и список должностей компании.

Правила:
- substages — подэтапы, содержанию которых фрагмент реально соответствует. Бери id ТОЛЬКО из
  списка. Может быть НЕСКОЛЬКО подэтапов или НИ ОДНОГО. confidence — уверенность 0..1.
- professions — должности из списка, для которых фрагмент специфичен. Если фрагмент годится
  всем — professions пустой, is_general=true.
- is_meaningful=false, если фрагмент служебный (заголовок, номер, оглавление) без содержания.
- why — одно короткое предложение-обоснование.
Этапы НЕ указывай — они выводятся из подэтапов. Отвечай строго по схеме, по-русски.

Пример пустого ответа (фрагмент ни к чему не подходит):
{"is_meaningful": true, "substages": [], "professions": [], "is_general": true, "why": "Общее вводное положение без привязки к подэтапу."}

Пример с тремя метками:
{"is_meaningful": true, "substages": [{"id":"safety.ppe","confidence":0.9},{"id":"firstday.equipment","confidence":0.7},{"id":"training.shift","confidence":0.6}], "professions": ["водитель"], "is_general": false, "why": "Нормы выдачи и применения СИЗ для водителя на смене."}"""


def _plan_lines(structure: dict) -> str:
    lines = []
    for st in (structure or {}).get("stages") or []:
        for sub in st.get("substages") or []:
            desc = (sub.get("description") or sub.get("brief") or "").strip()
            lines.append(f"- {sub.get('id')} [{st.get('title')} / {sub.get('title')}]: {desc}")
    return "\n".join(lines)


def section_labels(section_text: str, heading_path: list, card: dict,
                   structure: dict, positions: list) -> dict:
    """Сырой JSON модели для секции (проход 2). Нормализацию делает core.coerce_section_labels."""
    user = (
        f"Карточка документа: {json.dumps(card, ensure_ascii=False)}\n"
        f"Путь заголовков: {' / '.join(heading_path or []) or '—'}\n"
        f"Должности компании: {', '.join(positions) or '—'}\n\n"
        f"Подэтапы плана:\n{_plan_lines(structure)}\n\n"
        f"Фрагмент:\n{(section_text or '')[:6000]}"
    )
    return _chat(SECTION_SYSTEM, user, SECTION_SCHEMA)
