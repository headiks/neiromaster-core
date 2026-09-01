---
tags: [project]
project: neiromaster
updated: 2026-08-13
---

# Архитектура: neiromaster

## Поток данных — вопрос сотрудника
1. `POST /ask` в `app.py` (нужна сессия) — session_id связывает вопросы в диалог
2. `rag.handle_question()`: анализ истории → классификация route/risk
3. Если `rag` → `topics.route_question()` выбирает 1-2 темы вектором
4. `search()` в Qdrant с фильтром по темам → `rerank()` → отбор по порогу
5. `generate_answer()` (qwen3:14b) пишет ответ строго по top-фрагментам
6. История диалога хранится в памяти процесса (`_conversation_history` в app.py)

## Поток данных — загрузка документа (асинхронно)
1. `POST /documents/upload` → `save_uploaded_file()` → `enqueue_document()` → 202 + job_id
2. Воркер-поток (FIFO) берёт файл: `index_document()` docling-конвертация → кэш
3. `topics.classify_document()`: быстрый вектор (score≥0.80) или LLM → тема/папка
4. Посекционный чанкинг (заголовки docling / номера пунктов) + эмбеддинг →
   Qdrant с payload `topic`+`section`. Фронт опрашивает `GET /documents/jobs/{id}`
5. Поиск ускорен keyword-индексами payload на `topic`/`source`/`section`

## Поток данных — план и расписание
1. `planner`: каталог этапов (`data/stage_catalog.json`) → план (plan.json)
2. `POST /plans/{id}/generate` → фоновая задача: по подэтапу поиск в Qdrant → текст
3. `employees.build_employee_schedule()`: план + дата выхода + плейсхолдеры → расписание
4. Экспорт в plan.json/md, schedule.json/md (send_at рассчитан, но не отправляется)

## Ключевые связи между модулями
- `app.py` — единственный HTTP-слой, тянет все модули
- `rag.py` (RAG) зависит от `topics.py` и `config.get_embedding`
- `topics.py` — и классификация документов, и маршрутизация вопросов (одна коллекция тем)
- `indexing.py` использует `topics.classify_document` при загрузке
- `employees.py` зависит от `planner` (расписание считается из плана)
- `auth.py` (сессии в Postgres, таблица `sessions`) + `users.py` (профили/роли в PostgreSQL через `db.py`) —
  разделены: auth не знает о профилях. Хранилище аккаунтов — SQL, не файлы JSON
- `app.py` startup — через `lifespan`: `db.init_schema()` → миграции → `ensure_owner`
- `config.py` — общие константы (Qdrant/Ollama хосты, эмбеддинг)

## Важные решения
- Темы = папка на диске + точка в Qdrant-коллекции `topics`: дешёвый вектор отсекает
  очевидное, LLM решает спорное, «Общие вопросы» ловит остаток — загрузка не падает из-за LLM
- Тексты сообщений зависят от документов, не от человека → один план на N сотрудников
- Сессии в Postgres (таблица `sessions`) → переживают рестарт, общие для всех воркеров; лок-аут перебора остаётся in-memory (короткоживущий)
- ПДн профиля (ФИО, контакты и т.п.) шифруются в БД Fernet-ом, если задан `NEIROMASTER_PII_KEY`; шифрование/расшифровка в choke-points `users._insert`/`_save_user`/`_row_to_user`, префикс `enc:` → старые открытые строки уживаются без миграции
- Вопрос без ответа не теряется: `app._route_to_human` в `/ask` при пустом ответе
  (escalate ЧС или rag без фрагментов) ставит его в очередь `questions.py`; админ
  отвечает через `/questions/{id}/resolve`, сотрудник видит ответ в `/api/my/questions`
