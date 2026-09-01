"""
Чистая логика пайплайна разметки — без сети, БД и Qdrant (поэтому тестируется целиком):
  - префильтр мусора регулярками (оглавление, номера пунктов, номера страниц, колонтитулы);
  - сегментация секции и мелкий чанкинг по границам предложений;
  - модель плана: id подэтапов, вывод этапов из подэтапов;
  - валидация/нормализация JSON от LLM (проход 2);
  - наследование меток секции в чанки.
Инфраструктурные слои (llm/store/qdrant_sink/pipeline) зовут эти функции.
"""

import re

# ---------- Оценка длины и предложения ----------
def est_tokens(text: str) -> int:
    """Грубая оценка числа токенов. Для русского ~3 символа на токен — с запасом.
    docpipe: эвристика; при желании заменить на реальный токенайзер модели."""
    return max(1, len((text or "").strip()) // 3)


_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[«\"“(\[A-ZА-ЯЁ0-9])")


def split_sentences(text: str) -> list:
    """Деление на предложения по границам .!?… + пробел + заглавная/кавычка/цифра.
    Переводы строк тоже считаются мягкими границами."""
    out = []
    for line in re.split(r"\n{2,}", (text or "").strip()):
        line = line.strip()
        if not line:
            continue
        out.extend(s.strip() for s in _SENT_SPLIT.split(line) if s.strip())
    return out


# ---------- Префильтр мусора (проход 0, до LLM) ----------
# Значения reject_reason — стабильные слаги для аналитики и повторной обработки.
_RE_CLAUSE_NUM = re.compile(r"^\s*\d+(?:\.\d+)*[.)]?\s*$")               # «5», «5.1», «5.1.2.»
_RE_PAGE_NUM = re.compile(r"^\s*(?:стр\.?|страница|page|—|-|–)?\s*\d+\s*(?:из\s*\d+)?\s*(?:—|-|–)?\s*$", re.I)
_RE_TOC_LEADER = re.compile(r".+?[.…]{4,}\s*\d+\s*$")               # «Раздел 3 ..... 12»
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)                       # «слово» — 2+ буквы


def prefilter(text: str) -> tuple:
    """(is_meaningful, reject_reason). False -> фрагмент до LLM НЕ доходит.
    Ловит: оглавление с dot-leader, одиночные номера пунктов, номера страниц, а также
    слишком короткие/несодержательные строки. Колонтитулы — отдельно (нужен контекст
    страниц), см. repeated_lines()."""
    t = (text or "").strip()
    if not t:
        return False, "empty"
    if _RE_TOC_LEADER.match(t):
        return False, "toc_leader"
    if _RE_CLAUSE_NUM.match(t):
        return False, "clause_number"
    if _RE_PAGE_NUM.match(t):
        return False, "page_number"
    letters = sum(ch.isalpha() for ch in t)
    if len(t) < 15 or letters < 10 or letters / len(t) < 0.35:
        return False, "low_content"
    if len(_WORD_RE.findall(t)) < 3:
        return False, "too_short"
    return True, None


def repeated_lines(pages: list, threshold: float = 0.6) -> set:
    """Колонтитулы: короткие строки, повторяющиеся более чем на threshold доле страниц.
    pages — список страниц, каждая — список строк. Возвращает множество строк-колонтитулов."""
    n = len(pages)
    if n < 3:
        return set()
    from collections import Counter
    cnt = Counter()
    for page in pages:
        seen = set()
        for line in page:
            s = (line or "").strip()
            if s and len(s) <= 80 and s not in seen:   # длинный абзац колонтитулом не бывает
                seen.add(s)
                cnt[s] += 1
    need = threshold * n
    return {s for s, c in cnt.items() if c > need}


# ---------- Сегментация секции и мелкий чанкинг ----------
def split_section_text(text: str, max_tokens: int = 1200) -> list:
    """Секция ≤ max_tokens идёт целиком; больше — режется по границам предложений на куски
    не длиннее max_tokens. Предложения не рвём."""
    text = (text or "").strip()
    if est_tokens(text) <= max_tokens:
        return [text] if text else []
    parts, buf = [], []
    for sent in split_sentences(text):
        trial = " ".join(buf + [sent])
        if buf and est_tokens(trial) > max_tokens:
            parts.append(" ".join(buf))
            buf = [sent]
        else:
            buf.append(sent)
    if buf:
        parts.append(" ".join(buf))
    return parts


def to_chunks(text: str, min_tokens: int = 200, max_tokens: int = 400, overlap_sentences: int = 1) -> list:
    """Мелкие чанки 200–400 токенов с перекрытием в одно предложение (для RAG).
    Границы — только по предложениям."""
    sents = split_sentences(text)
    if not sents:
        return []
    chunks, buf = [], []
    for sent in sents:
        buf.append(sent)
        if est_tokens(" ".join(buf)) >= max_tokens:
            chunks.append(" ".join(buf))
            buf = buf[-overlap_sentences:] if overlap_sentences else []
    tail = " ".join(buf).strip()
    if tail:
        # хвост меньше минимума приклеиваем к предыдущему чанку, если он есть и без него хвост куцый
        if chunks and est_tokens(tail) < min_tokens and buf[overlap_sentences:]:
            chunks[-1] = chunks[-1] + " " + " ".join(buf[overlap_sentences:])
        elif tail not in chunks:
            chunks.append(tail)
    return chunks


# ---------- Модель плана: подэтапы -> этапы ----------
def substage_parent_map(structure: dict) -> dict:
    """{substage_id: stage_id} по структуре плана {stages:[{id, substages:[{id}...]}]}."""
    out = {}
    for st in (structure or {}).get("stages") or []:
        for sub in st.get("substages") or []:
            if sub.get("id"):
                out[sub["id"]] = st.get("id")
    return out


def valid_substage_ids(structure: dict) -> set:
    return set(substage_parent_map(structure).keys())


def stages_from_substages(substage_ids, structure: dict) -> list:
    """Этапы выводятся из подэтапов через родительскую связь (у модели этапы не спрашиваем)."""
    parent = substage_parent_map(structure)
    out = []
    for sid in substage_ids or []:
        st = parent.get(sid)
        if st and st not in out:
            out.append(st)
    return out


# ---------- Валидация/нормализация ответа модели (проход 2) ----------
def coerce_section_labels(raw: dict, structure: dict) -> dict:
    """Приводит сырой JSON модели к нормализованной метке секции.
    - substages: оставляем только id, существующие в плане; confidence -> float 0..1;
    - stages выводятся из подэтапов (не из ответа модели);
    - professions — как есть (матч со штаткой делает вызывающий слой, эмбеддингом);
    - is_meaningful/is_general/why нормализуются.
    Ничего не поднимает — на кривом входе отдаёт безопасные значения."""
    raw = raw or {}
    valid = valid_substage_ids(structure)

    subs = []
    seen = set()
    for item in raw.get("substages") or []:
        if isinstance(item, dict):
            sid = str(item.get("id") or "").strip()
            conf = item.get("confidence")
        else:
            sid, conf = str(item).strip(), None
        if sid and sid in valid and sid not in seen:
            seen.add(sid)
            try:
                c = float(conf)
            except (TypeError, ValueError):
                c = 1.0
            subs.append({"id": sid, "confidence": max(0.0, min(1.0, c))})

    professions = [str(p).strip() for p in (raw.get("professions") or []) if str(p).strip()]
    is_general = bool(raw.get("is_general")) or (not professions and not raw.get("professions"))
    # если модель дала профессии — не общий; если явно general — профессии игнорируем
    if raw.get("is_general") is True:
        professions, is_general = [], True
    elif professions:
        is_general = False

    return {
        "is_meaningful": bool(raw.get("is_meaningful", True)),
        "substages": subs,
        "stages": stages_from_substages([s["id"] for s in subs], structure),
        "professions": professions,
        "is_general": is_general,
        "why": (str(raw.get("why") or "")).strip()[:500],
    }


# ---------- Наследование меток секции в чанк ----------
_INHERIT_KEYS = ("is_meaningful", "substages", "stages", "professions", "is_general")


def inherit_labels(section_label: dict) -> dict:
    """Метки чанка = копия меток родительской секции (source=inherited). Не пересчитываем."""
    src = section_label or {}
    out = {k: src.get(k) for k in _INHERIT_KEYS}
    out["substages"] = list(out.get("substages") or [])
    out["stages"] = list(out.get("stages") or [])
    out["professions"] = list(out.get("professions") or [])
    out["is_meaningful"] = bool(out.get("is_meaningful", True))
    out["is_general"] = bool(out.get("is_general", False))
    out["source"] = "inherited"
    return out
