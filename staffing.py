"""
staffing.py — импорт любой таблицы штатки/штатного расписания в профили сотрудников.

Единый вывод для ЛЮБОГО файла — ровно четыре поля: ФИО, Должность, Отдел, Дата
приёма/выхода. Никаких других столбцов.

Подход УНИВЕРСАЛЬНЫЙ, не заточен под конкретные файлы: столбцы определяет модель
(tablemap), а маршрут каждой строки строится по СМЫСЛУ ячейки. Каждую значимую ячейку
малая модель классифицирует в один из классов:
  person   — ФИО человека        -> строка данных, профиль сотрудника;
  position — должность/профессия  -> строка данных, профиль-вакансия (ФИО заполнят позже);
  org      — подразделение/отдел  -> строка-баннер, значение протягивается в «Отдел»;
  other    — прочее (шапка, итог, число, дата, примечание) -> пропускается.
Классификация идёт БАТЧАМИ (по десяткам уникальных ячеек за один запрос) — быстро и
не зависит от языка/словарей. Никаких жёстко зашитых списков должностей/подразделений.

Импорт: строка с ФИО -> профиль (логин по ФИО, временный пароль); без ФИО -> вакансия.
"""

import os
import re
import json
import secrets

import requests

import tablemap
import users
from config import OLLAMA_URL

# Единая выходная схема — 4 поля, и только они.
UNIFIED_FIELDS = [
    {"name": "full_name",  "description": "ФИО человека (фамилия имя отчество), если в файле есть люди"},
    {"name": "position",   "description": "краткое НАЗВАНИЕ должности/профессии (например «Главный геолог», «Электросварщик 5 разряда»). НЕ колонка с описанием обязанностей и НЕ категория персонала (Рабочие/Специалисты/Руководители)"},
    {"name": "department", "description": "название подразделения/отдела/участка; часто отдельная строка-баннер над блоком, а не столбец"},
    {"name": "start_date", "description": "дата приёма или выхода на работу"},
]
FIELD_KEYS = ["full_name", "position", "department", "start_date"]

# Классификация ячеек — большой моделью (точнее отличает должность от описания
# обязанностей и категории персонала, надёжнее ловит ФИО). Медленнее, идёт батчами.
CLASSIFY_MODEL = "qwen3:14b"
BATCH_SIZE = 40

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


# ---------- Словарь ФИО (детерминированная проверка на человека) ----------
# Три списка (Имя / Отчество / Фамилия). Если ячейка похожа на ФИО И хотя бы один её
# полнословный токен есть в словаре — это ЧЕЛОВЕК, без обращения к модели. Ловит людей,
# которых классификатор мог пропустить или принять за должность.
_NAME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "name_dict")
_NAME_FILES = ("first_names.txt", "middle_names.txt", "last_names.txt")
_NAME_SET = None

# капитализированное кириллическое слово (допускаем дефис: «Мурат-оол»); инициал «И.»/«И.О.»
_NAME_WORD = re.compile(r"^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?$")
_NAME_INIT = re.compile(r"^[А-ЯЁ]\.?(?:[А-ЯЁ]\.?)?$")


def _name_set() -> set:
    global _NAME_SET
    if _NAME_SET is None:
        s = set()
        for fn in _NAME_FILES:
            try:
                with open(os.path.join(_NAME_DIR, fn), encoding="utf-8") as f:
                    s.update(ln.strip().lower() for ln in f if ln.strip())
            except OSError:
                pass
        _NAME_SET = s
    return _NAME_SET


def looks_like_person(text: str) -> bool:
    """True, если ячейка — ФИО: 2–4 токена, ВСЕ похожи на имя (слово с заглавной или
    инициал) И хотя бы одно полное слово есть в словаре Имён/Отчеств/Фамилий."""
    toks = [t for t in re.split(r"\s+", (text or "").strip()) if t]
    if not (2 <= len(toks) <= 4):
        return False
    words = [t for t in toks if _NAME_WORD.match(t)]
    if not words or any(not (_NAME_WORD.match(t) or _NAME_INIT.match(t)) for t in toks):
        return False
    known = _name_set()
    return any(w.lower() in known for w in words)


# ---------- Логины/пароли ----------
def _translit(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (word or "").lower().translate(_TRANSLIT))


def username_base(full_name: str) -> str:
    """«Иванов Иван Иванович» -> «ivanov_i_i». Фамилия + инициалы, длина >= 3."""
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if not parts:
        return "user"
    surname = _translit(parts[0])
    initials = [_translit(p)[:1] for p in parts[1:] if _translit(p)]
    base = "_".join([surname] + initials) if surname else "_".join(initials)
    base = base.strip("_") or "user"
    while len(base) < 3:
        base += "0"
    return base


def _unique_username(base: str, taken: set) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}{i}" in taken:
        i += 1
    return f"{base}{i}"


def _temp_password(length: int = 10) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------- Классификация ячеек малой моделью (батчами) ----------
CLASSES = ("person", "position", "org", "other")

BATCH_SYSTEM = """Ты классифицируешь ячейки таблицы штатного расписания. Для каждого
пронумерованного элемента верни ОДИН класс:
person — ФИО конкретного человека (фамилия/имя/отчество);
position — краткое НАЗВАНИЕ должности или профессии;
org — организационная единица (подразделение, отдел, департамент, служба, цех, участок, организация);
other — всё прочее: описание обязанностей (целое предложение), категория персонала
        (Рабочие/Специалисты/Руководители/Управленческий персонал), заголовок, итог, число, дата, код.

Примеры:
«Главный геолог» -> position
«Электросварщик ручной сварки, 5 разряд» -> position
«Руководит деятельностью технических служб» -> other  (это описание обязанностей, не должность)
«Специалисты», «Управленческий персонал», «Рабочие» -> other  (категория персонала)
«Обособленное подразделение Рудник», «Механо-монтажный участок» -> org
«Иванов Иван Иванович» -> person
«Итого», «24», «15.05.2026», «2290000а-14612» -> other

Верни ТОЛЬКО JSON-массив строк той же длины и в том же порядке, например:
["person","org","position","other"]. Без пояснений."""


def _classify_llm(system: str, user: str) -> str:
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": CLASSIFY_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False, "think": False,
    }, timeout=300)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def _parse_labels(text: str, n: int) -> list:
    text = re.sub(r"```json\s*|```", "", text)
    i = text.find("[")
    if i == -1:
        raise ValueError("нет JSON-массива")
    arr, _ = json.JSONDecoder().raw_decode(text[i:])
    return [x if x in CLASSES else "other" for x in arr][:n]


def _classify_batch(chunk: list) -> dict:
    """Классифицирует список текстов -> {текст: класс}. При сбое модели — деградация в
    'position' (получаем вакансии, но не выдаём мусор за людей)."""
    body = "\n".join(f"{i + 1}. {t[:120]}" for i, t in enumerate(chunk))
    try:
        labels = _parse_labels(_classify_llm(BATCH_SYSTEM, body), len(chunk))
    except Exception:
        return {t: "position" for t in chunk}
    return {t: (labels[i] if i < len(labels) else "other") for i, t in enumerate(chunk)}


def classify_cells(texts, classifier=None) -> dict:
    """Классы для набора уникальных текстов. classifier(chunk)->{text:class} можно
    подменить в тестах."""
    classifier = classifier or _classify_batch
    uniq = sorted({(t or "").strip() for t in texts if (t or "").strip()})
    out = {}
    for i in range(0, len(uniq), BATCH_SIZE):
        out.update(classifier(uniq[i:i + BATCH_SIZE]))
    return out


# ---------- Разбор в единую схему ----------
def _cell(row, col):
    return (row[col].strip() if (col is not None and col < len(row) and row[col] is not None) else "")


def _guess_subject_col(grid: list, start: int) -> int:
    """Колонка с наибольшим числом текстовых (буквенных, не числовых) ячеек — обычно там
    названия должностей/подразделений/людей. Фолбэк, когда модель не нашла нужную колонку."""
    import collections
    cnt = collections.Counter()
    for row in grid[start:start + 60]:
        for c, v in enumerate(row):
            v = (v or "").strip()
            if v and any(ch.isalpha() for ch in v):
                cnt[c] += 1
    return cnt.most_common(1)[0][0] if cnt else 0


def extract_unified(grid: list, mapping: dict, classifier=None) -> list:
    """Единые записи [{full_name, position, department, start_date}]. Маршрут строки — по
    КЛАССУ её ячеек (person/position/org), а не по тому, как модель угадала назначение
    столбца. Колонки ФИО и должности выбираются робастно: из кандидатов (разметка модели +
    текстовая колонка) берётся та, где реально больше person / position. Отдел протягивается
    из строк-баннеров (org)."""
    cols = mapping.get("columns") or {}
    sections = mapping.get("sections") or {}
    start = mapping.get("data_start_row") or 0
    date_c = cols.get("start_date")
    dept_c = cols.get("department")
    rows = grid[start:]
    subj_c = _guess_subject_col(grid, start)

    # кандидаты столбцов, которые классифицируем: разметка модели + текстовая колонка
    cand_cols = {c for c in (cols.get("full_name"), cols.get("position"), subj_c,
                             sections.get("department"), dept_c) if c is not None}
    texts = set()
    for row in rows:
        for c in cand_cols:
            t = _cell(row, c)
            if t and not looks_like_person(t):   # ФИО из словаря модели не показываем
                texts.add(t)
    labels = classify_cells(texts, classifier)

    def lab(t):
        t = (t or "").strip()
        if not t:
            return None
        if looks_like_person(t):                 # словарь ФИО важнее вердикта модели
            return "person"
        return labels.get(t)

    def count(col, cls):
        return -1 if col is None else sum(1 for row in rows if lab(_cell(row, col)) == cls)

    # эффективные колонки: где реально больше нужного класса
    name_c = max([cols.get("full_name"), subj_c], key=lambda c: count(c, "person"))
    if count(name_c, "person") <= 0:
        name_c = None
    pos_c = max([cols.get("position"), subj_c], key=lambda c: count(c, "position"))
    if count(pos_c, "position") <= 0:
        pos_c = None
    dept_sec = sections.get("department")
    if dept_sec is None:
        dept_sec = subj_c if count(subj_c, "org") > 0 else None

    # Перепроверка «выпавшей» строки: если по колонкам-кандидатам в строке не нашлось ни
    # человека, ни должности, ни подразделения, а текст в строке есть — классифицируем ВСЕ её
    # непустые ячейки (не только кандидатные колонки) и добираем классы. Так ловим сдвиг, когда
    # ФИО или должность стоит в неожиданной колонке (объединённые ячейки, разъехавшийся шаблон).
    def _ensure_labels(extra):
        missing = [t for t in extra if t and t not in labels and not looks_like_person(t)]
        if missing:
            labels.update(classify_cells(missing, classifier))

    def row_classes(row, recheck=True):
        """{class: text} для строки: первый person/position/org среди колонок-кандидатов.
        Если человек не найден, а в строке есть НЕ разобранная колонка с текстом — перепроверяем
        (классифицируем ещё не размеченные ячейки строки): так ловим сдвиг ФИО/должности в
        неожиданную колонку. Разметка кэшируется (дедуп по тексту) — перепроверка не бьёт по
        каждой строке заново."""
        found = {}
        examined = set()
        for c in cand_cols:
            t = _cell(row, c)
            examined.add(t)
            cl = lab(t)
            if t and cl in ("person", "position", "org"):
                found.setdefault(cl, t)
        if recheck and "person" not in found:
            extra = [(v or "").strip() for v in row
                     if (v or "").strip() and any(ch.isalpha() for ch in v) and (v or "").strip() not in examined]
            if extra:
                _ensure_labels(extra)
                for t in extra:
                    cl = lab(t)
                    if cl in ("person", "position", "org"):
                        found.setdefault(cl, t)
        return found

    carried = ""
    out = []
    for row in rows:
        date = _cell(row, date_c)
        cbc = row_classes(row)
        # отдел из явной колонки, если это не колонка ФИО/должности/секции
        dept_col = _cell(row, dept_c) if (dept_c is not None and dept_c not in (pos_c, name_c, dept_sec)) else ""
        # приоритет: человек -> вакансия-должность -> баннер-подразделение
        if "person" in cbc:
            out.append({"full_name": cbc["person"], "position": cbc.get("position", ""),
                        "department": dept_col or carried, "start_date": date})
        elif "position" in cbc:
            out.append({"full_name": "", "position": cbc["position"],
                        "department": dept_col or carried, "start_date": date})
        elif "org" in cbc:
            carried = cbc["org"]
        # ничего значимого -> пропуск
    return out


def parse_file(source, filename: str = None, classifier=None) -> dict:
    """Разбор xlsx/xls/csv -> {"mapping", "records"} в единой схеме (4 поля)."""
    grid = tablemap.read_table_grid(source, filename)
    mapping = tablemap.map_columns(grid, UNIFIED_FIELDS)
    records = extract_unified(grid, mapping, classifier)
    return {"mapping": mapping, "records": records, "count": len(records)}


# ---------- Создание профилей/вакансий ----------
def import_records(records: list) -> dict:
    """Строки с ФИО -> профили сотрудников (логин/пароль в ответе, один раз).
    Строки без ФИО -> профили-вакансии «(вакансия) <должность>» без логина.
    Возвращает {"profiles": [...], "vacancies": [...], "skipped": [...]}."""
    existing = users.list_users()
    taken = {u["username"] for u in existing if u.get("username")}
    seen = {(u.get("full_name") or "").strip().lower() for u in existing if u.get("full_name")}

    profiles, vacancies, skipped = [], [], []
    for rec in records:
        name = (rec.get("full_name") or "").strip()
        position = (rec.get("position") or "").strip()
        department = (rec.get("department") or "").strip()
        date = (rec.get("start_date") or "").strip()

        if name:
            if name.lower() in seen:
                skipped.append({"full_name": name, "reason": "уже есть"})
                continue
            username = _unique_username(username_base(name), taken)
            password = _temp_password()
            try:
                users.create_user(
                    {"username": username, "password": password, "full_name": name,
                     "position": position, "department": department, "start_date": date or None},
                    role=users.ROLE_EMPLOYEE, must_change_credentials=True)
            except ValueError as e:
                skipped.append({"full_name": name, "reason": str(e)})
                continue
            taken.add(username)
            seen.add(name.lower())
            profiles.append({"full_name": name, "username": username,
                             "password": password, "position": position})
        elif position:
            users.create_user(
                {"full_name": f"(вакансия) {position}", "position": position,
                 "department": department, "notes": "Вакансия из штатного расписания."},
                role=users.ROLE_EMPLOYEE)
            vacancies.append({"position": position, "department": department})
    return {"profiles": profiles, "vacancies": vacancies, "skipped": skipped}
