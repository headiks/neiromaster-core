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
def _hard_split(unit: str, max_tokens: int) -> list:
    """Аварийная нарезка одного «неделимого» куска (нет границ предложений — таблица,
    список, docx без точек) по словам на части ≤ max_tokens. Гарантирует, что ни один
    кусок не превысит окно — иначе большие документы обрезались бы в промпте LLM."""
    words = (unit or "").split()
    if not words:
        return []
    parts, buf = [], []
    for w in words:
        buf.append(w)
        if est_tokens(" ".join(buf)) >= max_tokens:
            parts.append(" ".join(buf))
            buf = []
    if buf:
        parts.append(" ".join(buf))
    return parts


def split_section_text(text: str, max_tokens: int = 1200) -> list:
    """Режет секцию на куски НЕ длиннее max_tokens. Сначала по границам предложений/абзацев;
    если отдельная единица всё равно длиннее окна (таблица, список без точек) — дорезаем по
    словам (_hard_split). Так ни одна секция не превысит max_tokens и не обрежется в промпте
    (раньше docx-таблица давала секцию на 12k токенов, и LLM видел лишь её начало)."""
    text = (text or "").strip()
    if not text:
        return []
    if est_tokens(text) <= max_tokens:
        return [text]
    parts, buf = [], []
    for sent in (split_sentences(text) or [text]):
        if est_tokens(sent) > max_tokens:            # единица сама больше окна — дорезаем по словам
            if buf:
                parts.append(" ".join(buf))
                buf = []
            parts.extend(_hard_split(sent, max_tokens))
            continue
        if buf and est_tokens(" ".join(buf + [sent])) > max_tokens:
            parts.append(" ".join(buf))
            buf = [sent]
        else:
            buf.append(sent)
    if buf:
        parts.append(" ".join(buf))
    return [p for p in parts if p.strip()]


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


# ---------- Разбиение блока на чанки по маркерам от модели (ТЗ: чанки рекомендует LLM) ----------
def chunks_from_markers(text: str, markers: list) -> list:
    """Режет ИСХОДНЫЙ text по маркерам начала кусков (первые слова каждого чанка от модели).
    Текст чанка — дословный срез исходника между маркерами (модель не переписывает текст,
    только указывает границы). Маркер ищем по его первым ~40 символам от текущей позиции.
    Возвращает [] если маркеры не сработали — вызывающий откатывается на to_chunks."""
    text = text or ""
    if not text.strip() or not markers:
        return []
    positions = []
    cursor = 0
    for m in markers:
        probe = " ".join((m or "").split())[:40]      # нормализуем пробелы, берём начало
        if not probe:
            continue
        idx = text.find(probe, cursor)
        if idx == -1:
            idx = text.find(probe[:20], cursor)        # запасной короткий якорь
        if idx == -1:
            continue
        positions.append(idx)
        cursor = idx + 1
    positions = sorted(set(positions))
    if not positions:
        return []
    if positions[0] != 0:
        positions.insert(0, 0)                          # хвост до первого маркера не теряем
    out = []
    for i, p in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        piece = text[p:end].strip()
        if piece:
            out.append(piece)
    return out


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
# Селективность подэтапов: модель на большом каталоге склонна вываливать почти весь список
# с убывающей уверенностью вместо выбора. Держим только уверенные и немного.
# ponytail: порог+кап — эвристика; настоящий потолок в том, что часть подэтапов каталога —
# это процессные шаги адаптации (тест, встреча с наставником), а не темы контента, поэтому
# документ-регламент к ним и не должен относиться. Порог поднять/опустить по калибровке.
SUBSTAGE_MIN_CONF = 0.6   # ниже — не привязываем
SUBSTAGE_MAX = 5          # максимум подэтапов на блок


def coerce_section_labels(raw: dict, structure: dict) -> dict:
    """Приводит сырой JSON модели к нормализованной метке секции.
    - substages: только валидные id с confidence >= SUBSTAGE_MIN_CONF, топ-SUBSTAGE_MAX по уверенности;
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
            c = max(0.0, min(1.0, c))
            if c >= SUBSTAGE_MIN_CONF:
                subs.append({"id": sid, "confidence": c})
    subs.sort(key=lambda x: x["confidence"], reverse=True)
    subs = subs[:SUBSTAGE_MAX]

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
    """Метки чанка = копия меток родительской секции (source=inherited). Не пересчитываем.
    Используется как фолбэк, если модель не дала per-chunk разметку (старый формат)."""
    src = section_label or {}
    out = {k: src.get(k) for k in _INHERIT_KEYS}
    out["substages"] = list(out.get("substages") or [])
    out["stages"] = list(out.get("stages") or [])
    out["professions"] = list(out.get("professions") or [])
    out["is_meaningful"] = bool(out.get("is_meaningful", True))
    out["is_general"] = bool(out.get("is_general", False))
    out["source"] = "inherited"
    return out


# ---------- Per-chunk разметка: каждый чанк несёт СВОИ подэтапы ----------
# Раньше чанк наследовал метки всей секции (весь блок → все его подэтапы). Из-за этого
# один конкретный чанк на подэтап был неотличим от соседних. Теперь модель размечает
# КАЖДЫЙ чанк отдельно (в том же вызове), и подэтап получают только релевантные чанки.
def _coerce_substages(raw_subs, valid: set) -> list:
    """Валидные подэтапы с confidence >= порога, топ-N по уверенности (та же логика, что у секции)."""
    subs, seen = [], set()
    for item in raw_subs or []:
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
            c = max(0.0, min(1.0, c))
            if c >= SUBSTAGE_MIN_CONF:
                subs.append({"id": sid, "confidence": c})
    subs.sort(key=lambda x: x["confidence"], reverse=True)
    return subs[:SUBSTAGE_MAX]


def coerce_chunk_label(raw_chunk: dict, structure: dict) -> dict:
    """Нормализует метку ОДНОГО чанка от модели: {substages:[{id,conf}], is_general}."""
    valid = valid_substage_ids(structure)
    subs = _coerce_substages((raw_chunk or {}).get("substages"), valid)
    is_general = bool((raw_chunk or {}).get("is_general")) and not subs
    return {
        "substages": subs,
        "stages": stages_from_substages([s["id"] for s in subs], structure),
        "is_general": is_general,
    }


def split_labeled_chunks(text: str, raw_chunks: list, structure: dict,
                         max_tokens: int = 1200) -> list:
    """
    Режет секцию на чанки по маркерам модели и вешает на каждый ЕГО метки.
    raw_chunks — [{marker, substages, is_general}] от LLM. Возвращает
    [{text, substages, stages, is_general}]. Текст — дословный срез исходника (модель
    указывает только границы, не переписывает). Слишком крупный чанк дорезаем по словам,
    метки наследуются на его части. Пусто/сбой маркеров -> фолбэк to_chunks без меток.
    """
    raw_chunks = raw_chunks or []
    markers = [(c.get("marker") if isinstance(c, dict) else c) or "" for c in raw_chunks]
    pieces = chunks_from_markers(text, markers)
    out = []
    if pieces and len(pieces) >= len(raw_chunks):
        # chunks_from_markers мог добавить «хвост до первого маркера» в начало — тогда
        # число кусков на 1 больше числа маркеров; этот головной кусок метим как общий.
        offset = len(pieces) - len(raw_chunks)
        for i, piece in enumerate(pieces):
            raw = raw_chunks[i - offset] if i >= offset else {}
            lbl = coerce_chunk_label(raw if isinstance(raw, dict) else {}, structure)
            for part in (split_section_text(piece, max_tokens) or [piece]):
                out.append({"text": part, **lbl})
        return out
    # маркеры не сработали — детерминированный фолбэк без per-chunk меток
    for part in to_chunks(text):
        out.append({"text": part, "substages": [], "stages": [], "is_general": False})
    return out


def fill_undecided_chunks(chunk_labels: list, section: dict) -> None:
    """
    Страховка от пропусков (мутирует chunk_labels на месте). Чанк, у которого модель НЕ
    проставила ни одного подэтапа и НЕ пометила его общим (is_general=False) — это «не
    решила»: типично для строк таблиц, продолжений перечней, формул. Такой чанк наследует
    подэтапы СЕКЦИИ (тему соседних чанков), чтобы информация не выпадала из подэтапа.
    Чанки, явно помеченные общими, НЕ трогаем — они и должны идти в общую базу.
    """
    sec_subs = section.get("substages") or []
    if not sec_subs:
        return                                   # у секции нет темы — наследовать нечего
    sec_stages = list(section.get("stages") or [])
    for ch in chunk_labels or []:
        if not (ch.get("substages") or []) and not ch.get("is_general"):
            ch["substages"] = [dict(s) for s in sec_subs]
            ch["stages"] = list(sec_stages)
            ch["source"] = "inherited"           # пометка: подэтап унаследован, не от модели


def section_from_chunks(chunk_labels: list, structure: dict, base: dict) -> dict:
    """Метка СЕКЦИИ = объединение меток её чанков (для доски и section_labels):
    подэтап секции = максимальная уверенность среди чанков; is_general — если ни один чанк
    не дал подэтапа, но есть общий по смыслу. base несёт секционные поля (is_meaningful,
    professions, why, prof_conf, reject_reason)."""
    best = {}
    for ch in chunk_labels or []:
        for s in ch.get("substages") or []:
            if s["confidence"] > best.get(s["id"], -1.0):
                best[s["id"]] = s["confidence"]
    subs = [{"id": sid, "confidence": c} for sid, c in best.items()]
    subs.sort(key=lambda x: x["confidence"], reverse=True)
    subs = subs[:SUBSTAGE_MAX]
    any_general = any(ch.get("is_general") for ch in chunk_labels or [])
    professions = list(base.get("professions") or [])
    return {
        "is_meaningful": bool(base.get("is_meaningful", True)),
        "substages": subs,
        "stages": stages_from_substages([s["id"] for s in subs], structure),
        "professions": professions,
        "is_general": (not subs) and any_general and not professions,
        "prof_conf": base.get("prof_conf"),
        "why": (str(base.get("why") or "")).strip()[:500],
        "reject_reason": base.get("reject_reason"),
    }
