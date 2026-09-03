"""
storage.py — durable-хранилище ОРИГИНАЛОВ документов.

Модель: если настроен S3 (config.S3_ENABLED), каждый оригинал дублируется в бакет,
а локальный data/documents/ работает как кэш — его можно потерять/очистить и
восстановить из S3 (pull). Так конвейер docling остаётся на локальных путях без
переписывания: перед обработкой файл при необходимости подтягивается из S3.

Производные (converted/, processed/) НЕ хранятся в S3 — они регенерируются из
оригинала, и держать их локально на каждом инстансе дешевле и проще.

boto3 импортируется лениво — нужен только при включённом S3 (локальная разработка
без S3 не требует зависимости). Секреты берутся из окружения через config.
"""

from pathlib import Path

import config

_client = None


def _s3():
    global _client
    if _client is None:
        import boto3  # ленивый импорт: без S3 зависимость не нужна
        _client = boto3.client(
            "s3",
            endpoint_url=config.S3_ENDPOINT,
            aws_access_key_id=config.S3_KEY,
            aws_secret_access_key=config.S3_SECRET,
            region_name=config.S3_REGION,
        )
    return _client


def _key(filename: str) -> str:
    return f"{config.S3_PREFIX}{Path(filename).name}"


def put(filename: str, content: bytes):
    """Залить оригинал в S3. No-op, если S3 выключен."""
    if config.S3_ENABLED:
        _s3().put_object(Bucket=config.S3_BUCKET, Key=_key(filename), Body=content)


def pull(filepath) -> bool:
    """Если локальной копии нет — скачать оригинал из S3 в этот путь.
    Возвращает True, если файл доступен локально после вызова. Без S3 — просто
    проверка существования локального файла."""
    filepath = Path(filepath)
    if filepath.exists():
        return True
    if not config.S3_ENABLED:
        return False
    filepath.parent.mkdir(parents=True, exist_ok=True)
    _s3().download_file(config.S3_BUCKET, _key(filepath.name), str(filepath))
    return True


def delete(filename: str):
    """Удалить оригинал из S3. No-op, если S3 выключен."""
    if config.S3_ENABLED:
        _s3().delete_object(Bucket=config.S3_BUCKET, Key=_key(filename))


def list_objects(prefix: str = "", delimiter: str = "/", max_keys: int = 1000) -> dict:
    """Листинг бакета для просмотра (только метаданные, без содержимого файлов).
    delimiter='/' — «папки» (CommonPrefixes) + файлы текущего уровня, как файловый браузер;
    delimiter='' — рекурсивно все ключи под prefix. Возвращает и endpoint/bucket, чтобы
    страница показывала, на каком сервере лежит хранилище."""
    base = {"enabled": config.S3_ENABLED, "bucket": config.S3_BUCKET,
            "endpoint": config.S3_ENDPOINT, "region": config.S3_REGION,
            "prefix": prefix, "folders": [], "files": [], "count": 0,
            "total_size": 0, "truncated": False}
    if not config.S3_ENABLED:
        return base
    kw = {"Bucket": config.S3_BUCKET, "Prefix": prefix, "MaxKeys": max_keys}
    if delimiter:
        kw["Delimiter"] = delimiter
    r = _s3().list_objects_v2(**kw)
    base["folders"] = [
        {"prefix": cp["Prefix"], "name": cp["Prefix"][len(prefix):].rstrip("/")}
        for cp in r.get("CommonPrefixes", [])
    ]
    files = []
    for o in r.get("Contents", []):
        key = o["Key"]
        if key == prefix:                       # сам маркер «папки» файлом не показываем
            continue
        lm = o.get("LastModified")
        files.append({"key": key, "name": key[len(prefix):],
                      "size": o.get("Size", 0),
                      "last_modified": lm.isoformat() if lm else ""})
    base["files"] = files
    base["count"] = len(files)
    base["total_size"] = sum(f["size"] for f in files)
    base["truncated"] = bool(r.get("IsTruncated"))
    return base


def check():
    """Проверка доступа к бакету: `python storage.py --check`."""
    if not config.S3_ENABLED:
        print("S3 выключен (нет NEIROMASTER_S3_ENDPOINT / NEIROMASTER_S3_BUCKET) — "
              "оригиналы хранятся только локально в data/documents/.")
        return
    _s3().head_bucket(Bucket=config.S3_BUCKET)
    print(f"S3 OK: {config.S3_ENDPOINT} bucket={config.S3_BUCKET} "
          f"region={config.S3_REGION} prefix={config.S3_PREFIX!r}")


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        check()
    else:
        # Смоук-тест ключа без сети: prefix корректно приклеивается, берётся basename.
        config.S3_PREFIX = "documents/"
        assert _key("a.pdf") == "documents/a.pdf"
        assert _key("/tmp/sub/b.docx") == "documents/b.docx"
        print("storage: _key — OK (для проверки S3-доступа: python storage.py --check)")
