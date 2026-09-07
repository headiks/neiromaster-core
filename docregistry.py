"""
Реестр документов — операционное состояние файлов базы знаний, ключ = имя файла.

Что хранит одна запись: жизненный цикл (status: uploaded/processing/indexed/error,
error, chunks), происхождение (uploaded_by* , department, storage_path, s3_key),
результат классификации (folders, stage_ids, summary), уточнения человека
(clarification) и пути производных (path, markdown_path).

Отношение к таблице document_meta (documents.py): это РАЗНЫЕ хранилища с разными
ролями, а не дубль. Здесь — жизненный цикл и провенанс, ключ по имени файла;
там — семантика для доски «этапы ↔ документы»: эмбеддинг документа (1024),
ключевые слова, привязка к подэтапам по косинусу, ключ по sha256. Общие поля
(folders/stage_ids) синхронизируются в одном месте — при индексации и переанализе
(indexing.reanalyze_document -> documents.update_assignment_by_filename), не разъезжаются.

Хранилище — JSON-файл (config.REGISTRY_PATH) под межпроцессной блокировкой
(config.FileGuard): read-modify-write безопасен и при нескольких uvicorn-воркерах.
ponytail: файловый store достаточен для одного узла; переезд в PostgreSQL —
когда появятся несколько серверов приложения на одну базу.
"""

import json

from config import REGISTRY_PATH, FileGuard

# Межпроцессная блокировка реестра. Раньше был обычный threading.Lock, который
# между процессами не действует — параллельные записи теряли обновления.
_lock = FileGuard(REGISTRY_PATH.with_suffix(".lock"))


def load() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save(reg: dict):
    tmp_path = REGISTRY_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    tmp_path.replace(REGISTRY_PATH)


def update(filename: str, **fields) -> dict:
    with _lock:
        reg = load()
        entry = reg.get(filename, {"filename": filename})
        entry.update(fields)
        reg[filename] = entry
        save(reg)
        return entry


def get(filename: str) -> dict | None:
    with _lock:
        return load().get(filename)


def remove(filename: str) -> bool:
    with _lock:
        reg = load()
        existed = reg.pop(filename, None) is not None
        save(reg)
    return existed


def list_documents() -> list:
    with _lock:
        reg = load()
    return sorted(reg.values(), key=lambda e: e.get("uploaded_at", ""), reverse=True)


def set_clarification(filename: str, text: str) -> bool:
    """Текстовое уточнение человека к документу (ТЗ §17). Сам документ не меняется —
    уточнение хранится в реестре как доп. контекст для ассистента."""
    with _lock:
        reg = load()
        if filename not in reg:
            return False
        reg[filename]["clarification"] = text
        save(reg)
    return True


def strip_folder(slug: str):
    """Убирает slug папки у всех документов (папку удалили/выключили). Сам документ
    остаётся в базе — снимается только принадлежность к категории (ТЗ §3)."""
    with _lock:
        reg = load()
        for doc in reg.values():
            if slug in (doc.get("folders") or []):
                doc["folders"] = [s for s in doc["folders"] if s != slug]
        save(reg)


def folder_doc_counts() -> dict:
    """slug папки -> сколько документов к ней отнесено. Папка — логическая метка,
    документ может входить сразу в несколько папок."""
    with _lock:
        reg = load()
    counts: dict[str, int] = {}
    for doc in reg.values():
        for slug in doc.get("folders") or []:
            counts[slug] = counts.get(slug, 0) + 1
    return counts


if __name__ == "__main__":
    # Самопроверка на временном файле: update мержит, remove удаляет, счётчик папок.
    import config
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "registry.json"
    config.REGISTRY_PATH = tmp
    globals()["REGISTRY_PATH"] = tmp
    _lock2 = FileGuard(tmp.with_suffix(".lock"))
    globals()["_lock"] = _lock2

    assert load() == {}
    update("a.pdf", status="uploaded", folders=["ot"])
    update("a.pdf", status="indexed", chunks=5)          # мерж, не перезапись
    e = get("a.pdf")
    assert e["status"] == "indexed" and e["folders"] == ["ot"] and e["chunks"] == 5
    update("b.pdf", status="indexed", folders=["ot", "pb"])
    assert folder_doc_counts() == {"ot": 2, "pb": 1}
    assert [d["filename"] for d in list_documents()] and set_clarification("a.pdf", "устарел")
    assert get("a.pdf")["clarification"] == "устарел"
    strip_folder("ot")
    assert get("b.pdf")["folders"] == ["pb"]      # ot снят, pb остался
    assert remove("a.pdf") and not remove("a.pdf") and get("a.pdf") is None
    print("docregistry: update/get/remove/counts/clarification/strip_folder — OK")
