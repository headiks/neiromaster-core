#!/usr/bin/env bash
# Полная установка всего, что нужно для работы RAG-ассистента на Linux-сервере:
#   - системные пакеты (сборка, рендеринг изображений для docling/OCR)
#   - Python-окружение и зависимости проекта (fastapi, docling, qdrant-client...)
#   - Qdrant (векторная БД) в Docker, автозапуск через --restart
#   - Ollama + модели: bge-m3 (эмбеддинги), qwen2.5:3b и qwen3:14b (классификация/генерация)
#   - systemd-сервисы, чтобы всё переживало обрыв SSH-сессии и перезагрузку сервера
#
# Запускать из корня распакованного проекта (там, где лежит app.py):
#   chmod +x install.sh && ./install.sh
#
# Работает и от root, и от обычного пользователя с sudo.
set -e

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi
export DEBIAN_FRONTEND=noninteractive
APP_DIR="$(pwd)"
APP_USER="$(whoami)"

echo "==> [1/8] Обновление списка пакетов"
$SUDO apt-get update -y

echo "==> [2/8] Системные зависимости"
# python3-venv/pip — окружение; build-essential — сборка колёс некоторых пакетов;
# libgl1/libglib2.0-0 — нужны OpenCV/EasyOCR, которые тянет docling для разбора PDF;
# curl — установка Ollama и Docker; ufw — управление файрволом сервера
$SUDO apt-get install -y \
    python3 python3-venv python3-pip \
    build-essential \
    libgl1 libglib2.0-0 \
    curl ufw

echo "==> [3/8] Виртуальное окружение и Python-зависимости"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
# --extra-index-url подтягивает CPU-сборку torch (нужна docling под капотом).
# Если на сервере есть NVIDIA GPU с настроенным CUDA — уберите этот флаг,
# чтобы pip поставил GPU-версию torch (разбор PDF будет заметно быстрее).
pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu

echo "==> [4/8] Docker + Qdrant (векторная БД)"
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | $SUDO sh
fi
$SUDO docker rm -f qdrant 2>/dev/null || true
# Порты привязаны только к 127.0.0.1: на сервере с публичным IP Qdrant
# не должен быть доступен снаружи, приложение обращается к нему через localhost.
# --restart unless-stopped — переживёт перезагрузку сервера.
$SUDO docker run -d --name qdrant --restart unless-stopped \
    -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
    -v "$APP_DIR/qdrant_storage:/qdrant/storage" \
    qdrant/qdrant

echo "==> [5/8] PostgreSQL (аккаунты и структурированные данные)"
# Секреты приложения (DSN с паролем БД) — в отдельном env-файле, он в .gitignore
# и подключается к systemd-сервису через EnvironmentFile. Пароль генерируется один раз.
ENV_FILE="$APP_DIR/.env.production"
if [ ! -f "$ENV_FILE" ]; then
    PG_PASS="$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | xxd -p)"
    cat > "$ENV_FILE" <<EOF
# Секреты приложения — НЕ коммитить (файл в .gitignore)
NEIROMASTER_DB_DSN=postgresql://neiromaster:${PG_PASS}@localhost:5432/neiromaster
EOF
    chmod 600 "$ENV_FILE"
fi
# Пароль для инициализации контейнера берём из уже записанного DSN (идемпотентно при повторном запуске)
source "$ENV_FILE"
PG_PASS="$(echo "$NEIROMASTER_DB_DSN" | sed -E 's#.*://[^:]+:([^@]+)@.*#\1#')"
# Порт только на 127.0.0.1 — наружу БД не торчит. Данные в томе pg_data (переживают пересоздание контейнера).
if ! $SUDO docker ps -a --format '{{.Names}}' | grep -qx neiromaster-pg; then
    $SUDO docker run -d --name neiromaster-pg --restart unless-stopped \
        -e POSTGRES_USER=neiromaster -e POSTGRES_PASSWORD="$PG_PASS" -e POSTGRES_DB=neiromaster \
        -p 127.0.0.1:5432:5432 \
        -v "$APP_DIR/pg_data:/var/lib/postgresql/data" \
        postgres:16-alpine
fi
echo "    Ожидание готовности PostgreSQL..."
for i in $(seq 1 60); do
    $SUDO docker exec neiromaster-pg pg_isready -U neiromaster >/dev/null 2>&1 && break
    sleep 1
done

echo "==> [6/8] Ollama (локальные LLM и эмбеддинги)"
if ! command -v ollama &> /dev/null; then
    curl -fsSL https://ollama.com/install.sh | $SUDO sh
fi
# Официальный установщик Ollama сам создаёт systemd-сервис ollama.service
# на порту 11434. Код проекта ждёт Ollama на 8080 — переопределяем порт
# через systemd drop-in (переживает обрыв SSH и перезагрузку, в отличие от nohup).
$SUDO mkdir -p /etc/systemd/system/ollama.service.d
cat <<'EOF' | $SUDO tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null
[Service]
Environment="OLLAMA_HOST=127.0.0.1:8080"
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now ollama
sleep 3

echo "==> [7/8] Загрузка моделей (может занять время — суммарно несколько ГБ)"
export OLLAMA_HOST=127.0.0.1:8080
ollama pull bge-m3        # эмбеддинги
ollama pull qwen2.5:3b    # классификация/реранк (быстрая модель)
ollama pull qwen3:14b     # генерация ответа (модель покрупнее, ~9+ ГБ RAM без GPU)

echo "==> [8/8] systemd-сервис для самого приложения (FastAPI)"
cat <<EOF | $SUDO tee /etc/systemd/system/rag-app.service > /dev/null
[Unit]
Description=RAG Assistant (FastAPI)
After=network.target docker.service ollama.service
Requires=docker.service ollama.service

[Service]
WorkingDirectory=$APP_DIR
# DSN к PostgreSQL с паролем — из защищённого env-файла (chmod 600, в .gitignore)
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/app.py
Restart=on-failure
User=$APP_USER

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable rag-app

echo ""
echo "==> Проверка сервисов"
$SUDO docker exec neiromaster-pg pg_isready -U neiromaster >/dev/null 2>&1 && echo "    Postgres OK (127.0.0.1:5432)" || echo "    Postgres НЕ отвечает"
curl -s http://127.0.0.1:6333/collections >/dev/null && echo "    Qdrant  OK (127.0.0.1:6333)" || echo "    Qdrant  НЕ отвечает"
curl -s http://127.0.0.1:8080/api/tags   >/dev/null && echo "    Ollama  OK (127.0.0.1:8080)" || echo "    Ollama  НЕ отвечает"

echo ""
echo "==> Готово."
echo ""
echo "Проиндексировать то, что уже лежит в data/documents (разово, вручную):"
echo "    source .venv/bin/activate && python index_documents.py"
echo ""
echo "Запуск сайта как systemd-сервиса (не зависит от SSH-сессии, переживёт reboot):"
echo "    sudo systemctl start rag-app"
echo "    sudo systemctl status rag-app"
echo "    sudo journalctl -u rag-app -f      # логи"
echo ""
echo "Если серверу нужен внешний доступ к сайту (порт 8000) — откройте его в firewall:"
echo "    sudo ufw allow 8000/tcp"
echo "Порты Qdrant (6333) и Ollama (8080) остаются на localhost — наружу их открывать не нужно."
