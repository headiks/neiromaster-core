# Переезд на новую версию (чистый лист)

Новая модель работы с документами: папки задаёт человек, ИИ классифицирует
документы внутрь них, поиск многоуровневый. Старые данные (документы, векторы с
прежней разметкой `topic`) несовместимы с новой моделью, поэтому базу документов и
Qdrant очищаем и наполняем заново. **Структура папок в Postgres не стирается** — она
управляется человеком (стартовая заведётся из `data/knowledge_seed.json`, если таблица
пуста).

Сервер: `root@beverly`, каталог `/root/neiromaster`, systemd-юнит `rag-app`,
Qdrant в Docker (REST на `localhost:6333`), Postgres (`neiromaster`), Ollama на `:8080`.

## Порядок действий

```bash
# 1. Остановить приложение
sudo systemctl stop rag-app

# 2. Обновить код
cd /root/neiromaster
git pull

# 3. Зависимости (на случай изменений)
source .venv/bin/activate
pip install -r requirements.txt

# 4. Очистить документы, конвертации, кэш docling и реестр
rm -rf data/documents/*  data/converted/*  data/processed/*
rm -f  data/registry.json

# 5. Очистить Qdrant — удаляем коллекции, они пересоздадутся при старте
for c in reglaments topics folder_tags doc_summaries; do
  curl -s -X DELETE "http://localhost:6333/collections/$c" >/dev/null && echo "drop $c"
done

# 6. Запустить и проверить
sudo systemctl start rag-app
sudo systemctl status rag-app --no-pager
```

При старте приложение:
- пересоздаёт коллекцию чанков `reglaments` с новыми payload-индексами
  (`folders`, `stage_ids`, `source`, `section`);
- заводит стартовые папки из `data/knowledge_seed.json`, если таблица `folders` пуста;
- строит векторы папок в коллекции `folder_tags` (нужен работающий Ollama —
  если он ещё не поднят, векторы соберутся при первом изменении папки в админке).

## Проверка

```bash
# коллекции на месте
curl -s http://localhost:6333/collections | python3 -m json.tool
# ожидаем: reglaments, folder_tags, doc_summaries (topics больше не нужна)

# логи старта
journalctl -u rag-app -n 40 --no-pager
```

В браузере: войти → «Админка» → вкладка «База знаний»:
- смысловые папки на месте (структура из сида), у каждой «0 док.»;
- документов нет.

Дальше: при необходимости поправить папки/критерии в админке, затем загрузить
документы — ассистент сам разложит их по папкам и добавит в общую базу «Все документы».

## Полный сброс структуры папок (опционально)

Если нужно стереть и саму структуру папок/этапов и пересобрать её из Excel-сида
заново, перед шагом 6 очистите таблицы (имя контейнера Postgres — обычно
`neiromaster-pg`; уточните `docker ps`):

```bash
docker exec -it neiromaster-pg psql -U neiromaster -d neiromaster -c "TRUNCATE folders, stages;"
```

После рестарта таблицы заполнятся из `data/knowledge_seed.json` (5 блоков-этапов,
11 папок с критериями).

## Откат

Версии выкладываются через PR в `main`. Откат — на предыдущий тег/коммит:

```bash
cd /root/neiromaster
git log --oneline -5
git checkout <предыдущий_commit>
sudo systemctl restart rag-app
```
