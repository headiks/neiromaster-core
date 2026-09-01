---
tags: [project]
project: neiromaster
updated: 2026-08-13
---

# Файлы: neiromaster

## `app.py`
FastAPI-сервер и единственный HTTP-слой. Страницы (/, /login, /register, /setup, /admin),
RAG-эндпоинт `/ask`, управление документами/папками, конструктор планов, пользователи/роли,
личный кабинет сотрудника. Держит память диалогов в процессе (`_conversation_history`).

## `rag.py`
Ядро RAG. `handle_question()`: анализ истории → классификация route (`rag`/`general`/`escalate`)
с risk_flag → маршрутизация по темам → поиск → rerank → генерация ответа (qwen3:14b).
Пороги уверенности и модели заданы константами вверху файла.

## `topics.py`
Авто-сортировка документов по темам и маршрутизация вопросов. Тема = папка на диске +
точка в Qdrant-коллекции `topics`.
- `classify_document()` — быстрый вектор (score≥0.80) иначе LLM; отказ → «Общие вопросы»
- `route_question()` — вектор вопроса → top-2 темы для фильтра поиска
- Пороги: `TOPIC_MATCH_THRESHOLD=0.80`, `TOPIC_ROUTE_THRESHOLD=0.35`

## `indexing.py`
Конвейер документа: docling-конвертация → кэш → классификация темы (вызывает `topics`)
→ посекционный чанкинг → эмбеддинг → запись в Qdrant. Также листинг, статистика папок, удаление.
- **Фоновая очередь:** `enqueue_document` + один воркер-поток (FIFO), `get_index_job`/`queue_status`.
  Загрузка через сайт не ждёт тяжёлый docling синхронно.
- **Посекционный чанкинг:** есть заголовки docling → секция = путь заголовков; нет →
  делит по номерам пунктов `CLAUSE_RE` (`sectionize_by_clause`, «5.1 ...»). Метка в payload `section`.
- **Оптимизация поиска:** `ensure_payload_indexes` — keyword-индексы на `topic`/`source`/`section`,
  чтобы фильтр по теме не шёл полным перебором (важно на 100-200 док).

## `planner.py`
Конструктор плана: загрузка каталога этапов, нормализация плана, расчёт относительных дат
(`resolve_schedule`), фоновая генерация текстов по подэтапам, перегенерация одного,
рендер plan.md/schedule.md. Источник истины — plan.json.

## `employees.py`
Персональное расписание сотрудника: `build_employee_schedule()` = план + дата выхода +
подстановка плейсхолдеров (`[Имя]`, `[ФИО]`, `[Должность]`). Зависит от `planner`.

## `questions.py`
Очередь вопросов без ответа (эскалация человеку). Хранилище `data/pending_questions.json`.
`record()` ставит вопрос сотрудника в очередь (reason `escalate` ЧС / `no_answer`),
`list_all`/`list_for_user`, `resolve()` — админ отвечает, ответ уходит в кабинет.
Вызывается из `app._route_to_human` в `/ask`, когда ответа нет.

## `db.py`
Подключение к PostgreSQL: пул соединений (`psycopg_pool`), схема (таблица `users`),
хелперы `query`/`execute`/`init_schema`, `configure()` для тестов. DSN из
`NEIROMASTER_DB_DSN` (по умолчанию локальный Postgres). Заменил файловое хранилище аккаунтов.

## `users.py`
Профили и роли (владелец/админ/сотрудник) в PostgreSQL (через `db.py`). Создание
карточек, регистрация, назначение плана, смена ролей, передача владения. Разовые
миграции в БД из старых `users.json` и `employees.json`. Публичный API прежний.
Свободный текст ПДн (ФИО, должность, контакт, наставник, руководитель, заметки)
шифруется в БД Fernet-ом при заданном `NEIROMASTER_PII_KEY` (choke-points
`_insert`/`_save_user`/`_row_to_user`, префикс `enc:`); без ключа — открытым текстом.

## `auth.py`
Аутентификация: scrypt-хэши паролей, сессии в Postgres (таблица `sessions`, токен в httponly-куке), защита
от перебора, смена пароля со сбросом сессий. Не знает о профилях — только логин/сессия.

## `config.py`
Общие константы: пути `data/`, хосты Qdrant/Ollama, размерность эмбеддинга,
функция `get_embedding()`.

## `index_documents.py`
CLI разовой пакетной индексации папки `data/documents`.

## `install.sh`
Установка на Linux-сервер: apt-пакеты → venv → Qdrant в Docker → Ollama как systemd +
модели → systemd-сервис `rag-app`. Ollama переопределён на порт 8080.

## `static/`
Фронтенд: `index.html` (кабинет сотрудника — чат + план), `admin.html` (админка),
`login.html` / `register.html` / `setup.html`, `app.css`.

## `data/`
`documents/` (оригиналы по папкам-темам), `converted/` + `processed/` (кэш docling),
`stage_catalog.json` (каталог этапов). Метаданные пользователей/планов — в JSON (не БД).
