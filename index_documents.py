"""
CLI для пакетной (офлайн) индексации регламентов, уже лежащих в папке
data/documents/. Для загрузки файлов через браузер используется веб-ручка
POST /documents/upload в app.py — она вызывает ту же логику (indexing.py),
но обрабатывает файл сразу при загрузке.

Использование:
    python index_documents.py                 # индексирует всё в data/documents
    python index_documents.py --recreate       # пересоздать коллекцию с нуля
    python index_documents.py --dir /path/to/other/folder
"""

import argparse
from pathlib import Path

from indexing import index_all_documents, DOCS_DIR


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Индексация регламентов через docling в Qdrant")
    parser.add_argument("--dir", default=str(DOCS_DIR), help="Папка с документами регламентов")
    parser.add_argument("--recreate", action="store_true", help="Пересоздать коллекцию перед индексацией")
    args = parser.parse_args()
    index_all_documents(docs_dir=Path(args.dir), recreate=args.recreate)
