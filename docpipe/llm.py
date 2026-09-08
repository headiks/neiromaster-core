"""
LLM-слой (Ollama, 14B): два прохода разметки со СТРОГИМ JSON через format-constraint
(JSON schema), temperature=0 и фиксированным seed — воспроизводимо. Валидацию/нормализацию
ответа делает core.coerce_section_labels (проход 2) и professions.match_to_staffing.
"""

import os
import json
import requests

from config import OLLAMA_URL

MODEL = "qwen3:14b"
SEED = 7
TIMEOUT = 600
PROMPT_VERSION = "docpipe-5"   # v5: модель размечает КАЖДЫЙ чанк своими подэтапами (per-chunk)

_HEAD_TOKENS = 3000   # сколько начала документа отдаём в проход 1 (≈ символов * 3)

# Контекст модели. Промпт разметки = полный каталог подэтапов (большой) + крупный фрагмент,
# при дефолтном num_ctx (2–4k) это молча обрезается и метки едут. Расширяем.
# ponytail: на CPU большой ctx тормозит — потолок кладём через env, дефолт с запасом под блок 1800т.
NUM_CTX = int(os.environ.get("NEIROMASTER_DOCPIPE_NUM_CTX", "8192"))
# Сколько символов фрагмента блока отдаём модели в промпте (должно вмещать крупный блок
# SECTION_MAX_TOKENS; ~3 символа на токен). Настраивается тем же env, что и на сервере.
FRAGMENT_CHARS = int(os.environ.get("NEIROMASTER_DOCPIPE_FRAGMENT_CHARS", "24000"))


def _chat(system: str, user: str, schema: dict) -> dict:
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False, "think": False,
        "format": schema,                                  # структурированный вывод (JSON schema)
        "options": {"temperature": 0, "seed": SEED, "num_ctx": NUM_CTX},
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


# ---------- Проход 2: разметка секции ПО ЧАНКАМ ----------
# Модель делит фрагмент на логически завершённые чанки и КАЖДОМУ проставляет свои подэтапы.
# Так один релевантный чанк попадает в подэтап, даже если соседние чанки — про другое.
_CHUNK_SUBSTAGE = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["id", "confidence"],
}
SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_meaningful": {"type": "boolean"},
        "professions": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
        "chunks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "marker": {"type": "string"},
                    "substages": {"type": "array", "items": _CHUNK_SUBSTAGE},
                    "is_general": {"type": "boolean"},
                },
                "required": ["marker", "substages", "is_general"],
            },
        },
    },
    "required": ["is_meaningful", "professions", "why", "chunks"],
}

SECTION_SYSTEM = """Ты размечаешь ФРАГМЕНТ внутреннего документа относительно плана адаптации.
Тебе дают карточку документа, путь заголовков, текст фрагмента, ПОЛНЫЙ список подэтапов плана
(с id и описанием) и список должностей компании.

ГЛАВНОЕ: раздели фрагмент на логически завершённые ЧАНКИ и размечай КАЖДЫЙ чанк отдельно.
Чанк = законченная по смыслу единица, несущая информацию (правило, процедура, определение,
перечень, норма). Не разрывай мысль, предложение, пункт или таблицу посередине.

Для КАЖДОГО чанка верни объект:
- marker — ДОСЛОВНО первые 6–10 слов этого чанка (скопируй из текста без изменений, ничего не
  сокращай и не перефразируй). Первый marker — самое начало фрагмента. Маркеры идут по порядку.
- substages — ТОЛЬКО те подэтапы, содержанию которых ИМЕННО ЭТОТ чанк ПРЯМО соответствует.
  Бери id ТОЛЬКО из списка. БУДЬ СТРОГ: обычно 0–2 подэтапа на чанк, максимум 3. Подэтап — если
  чанк РАСКРЫВАЕТ его по существу (порядок, правило, норма, процедура), а не просто упоминает
  тему вскользь. confidence — честная уверенность 0..1 (НЕ порядковый номер); сомневаешься —
  не включай. Общая фраза «всем обеспечивается …» без раскрытия — это НЕ подэтап.
- is_general=true, если чанк осмысленный, но не раскрывает ни одного подэтапа (общая справка,
  вводное положение) — такой текст идёт в базу для ответов на вопросы, а не в покрытие подэтапа.
  Если у чанка есть substages — is_general=false.

Секция целиком:
- is_meaningful=false, если ВЕСЬ фрагмент служебный (заголовок, номер, оглавление) без содержания.
- professions — должности из списка, для которых специфичен весь фрагмент; иначе пустой.
- why — одно короткое предложение: о чём фрагмент.
Этапы НЕ указывай — они выводятся из подэтапов. Отвечай строго по схеме, по-русски.

Пример:
{"is_meaningful": true, "professions": ["водитель"], "why": "Порядок выдачи СИЗ и общее вводное положение.", "chunks": [{"marker": "Каждый работник обязан использовать средства", "substages": [{"id":"first_day.equipment_issue","confidence":0.9}], "is_general": false}, {"marker": "Настоящее положение разработано в соответствии", "substages": [], "is_general": true}]}"""


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
        f"Фрагмент:\n{(section_text or '')[:FRAGMENT_CHARS]}"
    )
    return _chat(SECTION_SYSTEM, user, SECTION_SCHEMA)
