#!/bin/sh
# Lanza Chromium con el perfil del canal y la política anti-fuga WebRTC.
# Usar SIEMPRE este lanzador para crear la cuenta / entrar en YouTube Studio.
set -eu

ACCOUNT="${GOOGLE_ACCOUNT:-${SLUG:-canal}}"
PROFILE="${TOKENS_DIR:-/data/tokens}/${ACCOUNT}_browser_profile"
CHROME="$(find /ms-playwright -maxdepth 3 -type f -name chrome | head -n 1)"

if [ -z "$CHROME" ]; then
    echo "Chromium no encontrado en /ms-playwright" >&2
    exit 1
fi

set -- \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --disable-blink-features=AutomationControlled \
    --user-data-dir="$PROFILE" \
    --no-first-run \
    --no-default-browser-check \
    --force-webrtc-ip-handling-policy=disable_non_proxied_udp \
    --proxy-bypass-list='<-loopback>' \
    "$@"

if [ -n "${EGRESS_PROXY_SERVER:-}" ]; then
    set -- --proxy-server="$EGRESS_PROXY_SERVER" "$@"
fi

exec "$CHROME" "$@"
