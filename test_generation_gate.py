"""Гейт генерации по наличию документа (planner.generate_substage_message).
Без документа, отнесённого к подэтапу (классификация docpipe) — подэтап пропускается,
LLM не зовётся. Заглушаем тяжёлые indexing/rag, чтобы проверить ветку skip без docling."""
import sys
import types

# Заглушки до импорта planner: функция внутри делает import indexing / from rag import ...
sys.modules.setdefault("indexing", types.SimpleNamespace(search_chunks=lambda *a, **k: []))
sys.modules.setdefault("rag", types.SimpleNamespace(
    big_llm=lambda *a, **k: "{}", parse_json_response=lambda s: {}))

import planner  # noqa: E402


def test_skip_when_no_document_for_substage():
    r = planner.generate_substage_message(
        {"catalog_id": "pre_onboarding"}, {"catalog_id": "greet_intro", "title": "Привет"},
        [], docs_present=set())
    assert r["status"] == "skipped"
    assert "документ" in (r["error"] or "").lower()


def test_skip_when_substage_has_no_catalog_id():
    # нет catalog_id -> сопоставить с классификацией нельзя -> пропуск
    r = planner.generate_substage_message(
        {}, {"title": "Кастомный подэтап"}, [], docs_present={"greet_intro"})
    assert r["status"] == "skipped"


if __name__ == "__main__":
    test_skip_when_no_document_for_substage()
    test_skip_when_substage_has_no_catalog_id()
    print("test_generation_gate: ветки пропуска — OK")
