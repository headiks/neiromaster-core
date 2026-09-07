# -*- coding: utf-8 -*-
"""
Единая таблица разнесения документов по этапам/подэтапам адаптации — для проверки
человеком в одном месте (просьба HR). Только чтение из БД (всё уже посчитано при
индексации/разметке), без обращений к Qdrant — работает за секунды.

Два листа = два независимых механизма привязки в системе:

  Лист 1 «Разметка по смыслам (LLM)» — docpipe: модель делит документ на блоки и
  для каждого решает, каким подэтапам он ПРЯМО соответствует, с обоснованием (why)
  и пометкой «общая информация» (is_general). Основной, более точный механизм.
  Источник: таблицы sections + section_labels.

  Лист 2 «Привязка по вектору (косинус)» — доска «этапы ↔ документы» в админке:
  сходство вектора документа с «запросом подэтапа». Тут возникает «индекс 0.61» и
  ложные срабатывания на общих фразах. Источник: document_meta.substages (score).

Запуск на сервере:  python export_classification.py
Результат:          Классификация_документов.xlsx
"""

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

import db
import docregistry
import documents
import stages
import docpipe.store as store

OUT = "Классификация_документов.xlsx"

HEAD = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="3B5B8C")
GEN_FILL = PatternFill("solid", fgColor="FFF3CD")      # общая информация — жёлтым
JUNK_FILL = PatternFill("solid", fgColor="EFEFEF")     # служебный блок — серым
LOW_FILL = PatternFill("solid", fgColor="FCE4E4")      # низкий score — розовым
WRAP = Alignment(wrap_text=True, vertical="top")


def _plan_luts():
    """id -> названия этапов и подэтапов из текущей версии плана."""
    structure = store.get_plan_structure("current")
    stage_lut, sub_lut = {}, {}
    for st in structure.get("stages") or []:
        stage_lut[st["id"]] = st.get("title", "")
        for sub in st.get("substages") or []:
            sub_lut[sub["id"]] = {"title": sub.get("title", ""), "stage_id": st["id"],
                                  "stage_title": st.get("title", "")}
    return stage_lut, sub_lut


def _stages_lut():
    """id подэтапа -> (название подэтапа, название этапа) из каталога stages —
    именно им пользуется косинус-привязка (document_meta), НЕ планом docpipe."""
    lut = {}
    for st in stages.list_stages():
        for sub in st.get("substages") or []:
            lut[sub["id"]] = {"title": sub.get("title", ""), "stage_title": st.get("title", "")}
    return lut


def _style_header(ws, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(1, c)
        cell.font = HEAD
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 30


def _wrap_body(ws):
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = WRAP


def sheet_llm(wb, stage_lut, sub_lut):
    ws = wb.active
    ws.title = "Разметка по смыслам (LLM)"
    cols = ["Документ", "Блок №", "Заголовок блока", "Тип блока", "Этап", "Подэтап",
            "Уверенность", "Профессии", "Обоснование (why)", "Текст блока (компонент)"]
    ws.append(cols)
    for i, w in enumerate([30, 7, 26, 18, 22, 32, 11, 18, 42, 62], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    docs = db.query("SELECT id, filename FROM documents ORDER BY filename", (), "all")
    for d in docs:
        rows = store.sections_with_labels(d["id"])
        for i, r in enumerate(rows, 1):
            heading = " / ".join(r.get("heading_path") or [])
            text = (r.get("text") or "").strip()[:1500]
            if r.get("is_meaningful") is False:
                ws.append([d["filename"], i, heading,
                           f"служебный: {r.get('reject_reason') or ''}", "", "", "", "", "", text])
                for c in range(1, len(cols) + 1):
                    ws.cell(ws.max_row, c).fill = JUNK_FILL
                continue
            profs = ", ".join(r.get("professions") or [])
            if r.get("is_general"):
                profs = profs or "общий для всех"
            why = r.get("why") or ""
            subs = r.get("substages") or []
            if not subs:
                label = "— общая информация —" if r.get("is_general") else "— подэтап не назначен —"
                ws.append([d["filename"], i, heading, "общий/без привязки", "", label,
                           "", profs, why, text])
                if r.get("is_general"):
                    for c in range(1, len(cols) + 1):
                        ws.cell(ws.max_row, c).fill = GEN_FILL
                continue
            for sname in subs:
                meta = sub_lut.get(sname.get("id"), {})
                conf = sname.get("confidence")
                ws.append([d["filename"], i, heading, "привязан",
                           meta.get("stage_title", ""), meta.get("title", "") or sname.get("id"),
                           round(conf, 2) if isinstance(conf, (int, float)) else conf,
                           profs, why, text])
    _wrap_body(ws)
    _style_header(ws, len(cols))
    return ws.max_row - 1


def sheet_cosine(wb, cos_lut):
    ws = wb.create_sheet("Привязка по вектору (косинус)")
    cols = ["Документ", "Этап", "Подэтап", "Score (индекс)", "Папки (смысловые блоки)",
            "Краткое описание документа"]
    ws.append(cols)
    for i, w in enumerate([34, 24, 34, 14, 34, 60], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    n = 0
    for m in documents.list_meta():
        subs = m.get("substages") or []
        folders = ", ".join(m.get("folders") or [])
        summ = (m.get("summary") or "").strip()[:400]
        if not subs:
            ws.append([m["filename"], "", "— вектор не дал уверенной привязки —", "", folders, summ])
            continue
        for a in subs:
            meta = cos_lut.get(a.get("substage_id"), {})
            score = a.get("score")
            title = meta.get("title") or a.get("title") or a.get("substage_id")
            ws.append([m["filename"], meta.get("stage_title", ""), title, score, folders, summ])
            if isinstance(score, (int, float)) and score < 0.45:
                for c in range(1, len(cols) + 1):
                    ws.cell(ws.max_row, c).fill = LOW_FILL
            n += 1
    _wrap_body(ws)
    _style_header(ws, len(cols))
    return n


def sheet_summary(wb):
    """Обзор по документам: сколько блоков, служебных, общих, с привязкой — для
    быстрой оценки полноты разметки (мало привязок у большого документа = недоработка)."""
    ws = wb.create_sheet("Сводка по документам")
    cols = ["Документ", "Блоков всего", "Служебных", "Общая информация",
            "С привязкой к подэтапу", "Уникальных подэтапов (LLM)", "Косинус-привязок"]
    ws.append(cols)
    for i, w in enumerate([40, 13, 12, 17, 22, 24, 16], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    cos_by_doc = {}
    for m in documents.list_meta():
        cos_by_doc[m["filename"]] = len(m.get("substages") or [])

    for d in db.query("SELECT id, filename FROM documents ORDER BY filename", (), "all"):
        rows = store.sections_with_labels(d["id"])
        junk = sum(1 for r in rows if r.get("is_meaningful") is False)
        gen = sum(1 for r in rows if r.get("is_meaningful") is not False
                  and not (r.get("substages") or []) and r.get("is_general"))
        attached = sum(1 for r in rows if r.get("substages"))
        uniq = len({s["id"] for r in rows for s in (r.get("substages") or [])})
        ws.append([d["filename"], len(rows), junk, gen, attached, uniq,
                   cos_by_doc.get(d["filename"], 0)])
    _wrap_body(ws)
    _style_header(ws, len(cols))


def sheet_legend(wb, n_llm, n_cos):
    ws = wb.create_sheet("Как читать", 0)
    ws.column_dimensions["A"].width = 118
    L = [
        ("Единая таблица разнесения документов по этапам и подэтапам адаптации", 14, True),
        ("", 11, False),
        ("В системе ДВА независимых механизма привязки документа к подэтапам — они на разных листах.", 11, True),
        ("", 11, False),
        ("Лист «Разметка по смыслам (LLM)» — основной, более точный.", 12, True),
        ("Модель делит документ на смысловые блоки и для каждого решает, каким подэтапам он ПРЯМО", 11, False),
        ("соответствует, с обоснованием (колонка «Обоснование»). Жёлтые строки — «общая информация»:", 11, False),
        ("блок осмысленный, но не раскрывает конкретный подэтап (место таким сведениям — в базе для", 11, False),
        ("ответов на вопросы, а не в покрытии подэтапа). Серые строки — служебные фрагменты.", 11, False),
        (f"Всего строк: {n_llm}.", 11, False),
        ("", 11, False),
        ("Лист «Привязка по вектору (косинус)» — то, что показывает доска «этапы ↔ документы» в админке.", 12, True),
        ("Это НЕ смысловой анализ, а близость векторов: «Score (индекс)» — тот самый индекс соответствия", 11, False),
        ("(например, 0.61). Высокий score = лишь ТЕМАТИЧЕСКАЯ близость, а не реальное раскрытие подэтапа.", 11, False),
        ("Именно здесь возникают ложные привязки вроде «ПВТР → Как формируется доход и KPI»: общая фраза", 11, False),
        ("про оплату труда векторно близка к теме дохода. Розовые строки — низкий score.", 11, False),
        (f"Всего строк: {n_cos}.", 11, False),
        ("", 11, False),
        ("Как проверять: опирайтесь на лист LLM (обоснование по каждому блоку). Лист косинуса — чтобы", 11, False),
        ("увидеть, откуда на доске берутся спорные привязки с индексом.", 11, False),
    ]
    for i, (text, size, bold) in enumerate(L, 1):
        c = ws.cell(i, 1, text)
        c.font = Font(bold=bold, size=size)
        c.alignment = Alignment(wrap_text=True, vertical="top")


if __name__ == "__main__":
    stage_lut, sub_lut = _plan_luts()
    cos_lut = _stages_lut()
    print(f"подэтапов: план docpipe {len(sub_lut)}, каталог stages {len(cos_lut)}")
    wb = openpyxl.Workbook()
    n_llm = sheet_llm(wb, stage_lut, sub_lut)
    n_cos = sheet_cosine(wb, cos_lut)
    sheet_summary(wb)
    sheet_legend(wb, n_llm, n_cos)
    wb.save(OUT)
    print(f"готово: {OUT} (LLM строк {n_llm}, косинус строк {n_cos})")
