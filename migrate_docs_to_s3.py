"""
Разовая заливка уже лежащих локально оригиналов (data/documents/) в S3.
Нужна один раз при переезде на S3 — новые загрузки попадают в бакет сами
(indexing.save_uploaded_file). Идемпотентно: повторный запуск просто перезальёт.

    python migrate_docs_to_s3.py          # залить все файлы из data/documents
    python migrate_docs_to_s3.py --dry    # показать, что было бы залито, ничего не менять

Требует заданных NEIROMASTER_S3_ENDPOINT / _BUCKET / _KEY / _SECRET (см. config.py).
"""
import sys

import config
import storage
from config import DOCS_DIR, SUPPORTED_EXT


def main():
    dry = "--dry" in sys.argv
    if not config.S3_ENABLED:
        print("S3 не настроен (нет NEIROMASTER_S3_ENDPOINT / NEIROMASTER_S3_BUCKET). Нечего делать.")
        return
    files = [p for p in DOCS_DIR.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXT]
    if not files:
        print(f"В {DOCS_DIR} нет файлов для заливки.")
        return
    print(f"{'[dry] ' if dry else ''}Заливка {len(files)} файлов в "
          f"{config.S3_BUCKET} ({config.S3_ENDPOINT}), префикс {config.S3_PREFIX!r}:")
    for p in files:
        print(f"  {p.name}")
        if not dry:
            storage.put(p.name, p.read_bytes())
    print("Готово." if not dry else "Ничего не залито (--dry).")


if __name__ == "__main__":
    main()
