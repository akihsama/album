#!/usr/bin/env bash
set -e

# 一键部署脚本：构建 app 并通过 cloudflared 暴露到公网（临时 trycloudflare URL）
# 使用方法：
#   chmod +x deploy_cloudflare.sh
#   ./deploy_cloudflare.sh
#
# 也可在运行前设置环境变量覆盖默认值:
#   PASSWORD=strongpass SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))') ./deploy_cloudflare.sh

PASSWORD=${PASSWORD:-changeme}
SECRET_KEY=${SECRET_KEY:-$(python - <<'PY'
import secrets,sys
print(secrets.token_urlsafe(24))
PY
)}
MAX_CONTENT_LENGTH=${MAX_CONTENT_LENGTH:-16777216}
APP_PORT=${APP_PORT:-8000}

echo "→ PASSWORD: ${PASSWORD}"
echo "→ SECRET_KEY: (hidden)"
echo "→ MAX_CONTENT_LENGTH: ${MAX_CONTENT_LENGTH}"
echo "→ APP_PORT: ${APP_PORT}"
echo

# 1) 准备 .env
if [ ! -f .env ]; then
  cat > .env <<EOF
PASSWORD=${PASSWORD}
SECRET_KEY=${SECRET_KEY}
MAX_CONTENT_LENGTH=${MAX_CONTENT_LENGTH}
UPLOAD_FOLDER=uploads
SESSION_COOKIE_SECURE=0
EOF
  echo "Created .env"
else
  echo ".env already exists — leaving it unchanged"
fi

# 2) 确保 uploads 目录存在
mkdir -p uploads
chmod 700 uploads
echo "Prepared uploads/"

# 3) 生成临时 docker-compose 文件（把 app 绑定到宿主机端口）
COMPOSE_FILE=docker-compose.cloud.yml
cat > ${COMPOSE_FILE} <<EOF
version: '3.8'
services:
  app:
    build: .
    restart: unless-stopped
    environment:
      - PASSWORD=
      - SECRET_KEY=
      - UPLOAD_FOLDER=/app/uploads
      - MAX_CONTENT_LENGTH=
      - SESSION_COOKIE_SECURE=0
    volumes:
      - ./uploads:/app/uploads
    ports:
      - "${APP_PORT}:8000"
EOF

# use shell variable expansion carefully inside heredoc to inject values
cat > ${COMPOSE_FILE} <<EOF
version: '3.8'
services:
  app:
    build: .
    restart: unless-stopped
    environment:
      - PASSWORD=${PASSWORD}
      - SECRET_KEY=${SECRET_KEY}
      - UPLOAD_FOLDER=/app/uploads
      - MAX_CONTENT_LENGTH=${MAX_CONTENT_LENGTH}
      - SESSION_COOKIE_SECURE=0
    volumes:
      - ./uploads:/app/uploads
    ports:
      - "${APP_PORT}:8000"
EOF

echo "Wrote ${COMPOSE_FILE}"

# 4) 启动 Docker Compose
echo "→ Building and starting the app with docker compose..."
docker compose -f ${COMPOSE_FILE} up -d --build

echo "App container started and bound to http://localhost:${APP_PORT}"

# 5) 下载并安装 cloudflared（若已经存在则跳过）
CF_BIN=$(command -v cloudflared || true)
if [ -x "${CF_BIN}" ]; then
  echo "cloudflared found at ${CF_BIN}"
  CF_CMD="${CF_BIN}"
else
  echo "cloudflared not found locally. Installing cloudflared..."
  ARCH=$(uname -m)
  DL_DEST="/usr/local/bin/cloudflared"
  TMP_LOCAL="./cloudflared"
  if [ "${ARCH}" = "x86_64" ] || [ "${ARCH}" = "amd64" ]; then
    URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
  else
    echo "Unsupported architecture ${ARCH}. Attempting to download linux-amd64 binary anyway."
    URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
  fi

  if sudo -n true 2>/dev/null; then
    sudo curl -L -o ${DL_DEST} ${URL}
    sudo chmod +x ${DL_DEST}
    CF_CMD=${DL_DEST}
    echo "cloudflared installed to ${DL_DEST}"
  else
    curl -L -o ${TMP_LOCAL} ${URL}
    chmod +x ${TMP_LOCAL}
    CF_CMD="${PWD}/${TMP_LOCAL}"
    echo "Downloaded cloudflared to ${CF_CMD} (no sudo)"
  fi
fi

# 6) 启动 cloudflared tunnel (ephemeral) 并 capture URL
LOGFILE=cloudflared.log
echo "Starting cloudflared tunnel (temporary URL). Logs -> ${LOGFILE}"
nohup ${CF_CMD} tunnel --url "http://localhost:${APP_PORT}" > ${LOGFILE} 2>&1 &

CF_PID=$!
echo "cloudflared PID: ${CF_PID}"
echo "Waiting up to 12s for the tunnel to become available..."

URL=""
for i in {1..12}; do
  sleep 1
  URL=$(grep -oE 'https?://[A-Za-z0-9.-]+trycloudflare\.com' ${LOGFILE} | tail -n1 || true)
  if [ -n "$URL" ]; then
    break
  fi
done

if [ -n "$URL" ]; then
  echo
  echo "Tunnel established!"
  echo "Public HTTPS URL: ${URL}"
  echo
  echo "Open the URL in browser, then /login to sign in with the PASSWORD you set."
  echo "To stop:"
  echo "  docker compose -f ${COMPOSE_FILE} down"
  echo "  kill ${CF_PID}"
else
  echo
  echo "未能在日志中获取到 tunnel URL。请查看 ${LOGFILE} 了解详情："
  echo "  tail -n +1 ${LOGFILE}"
  echo
  echo "如需交互式调试，请运行:"
  echo "  ${CF_CMD} tunnel --url \"http://localhost:${APP_PORT}\""
  echo "这将直接在终端输出 URL 和日志。"
fi
