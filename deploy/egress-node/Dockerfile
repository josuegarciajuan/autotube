# Gateway egress por canal: agente + escritorio VNC + Chromium.
#
# Base: imagen oficial de Playwright (Ubuntu 22.04 + Chromium 131 + deps).
# El agente NO abre la DB del servidor principal: AUTOTUBE_AGENT_MODE=1.
# Los secretos/perfiles viven en /data (volumen por canal), nunca en la imagen.
FROM mcr.microsoft.com/playwright:v1.49.0-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    AUTOTUBE_AGENT_MODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Madrid

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        xvfb x11vnc xfce4-session xfce4-terminal dbus-x11 \
        fonts-dejavu-core ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir \
        fastapi uvicorn python-multipart \
        google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib \
        httplib2 requests python-dotenv \
        playwright==1.49.0 yt-dlp

WORKDIR /app
COPY . /app

RUN chmod +x /app/deploy/egress-node/entrypoint.sh \
             /app/deploy/egress-node/navegador.sh && \
    mkdir -p /data/tokens /data/staging /data/home && \
    chown -R 1000:1000 /data

# El contenedor corre como UID 1000 (pwuser de la imagen base).
# /app queda de solo lectura para el proceso; escribe en /data y /tmp.
ENV TOKENS_DIR=/data/tokens \
    YT_BROWSER_TOKENS_DIR=/data/tokens \
    GOOGLE_CLIENT_SECRET_PATH=/data/client_secret.json \
    OUTPUT_DIR=/tmp/output \
    DATABASE_PATH=/tmp/agent.db \
    HOME=/data/home \
    DISPLAY=:10 \
    SLUG=canal6

EXPOSE 9101 5900

ENTRYPOINT ["/app/deploy/egress-node/entrypoint.sh"]
