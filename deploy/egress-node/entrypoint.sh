#!/bin/sh
# Arranca el escritorio VNC y el agente egress en el mismo contenedor.
# Si el agente muere, el contenedor termina y systemd/Docker lo reinicia.
set -eu

mkdir -p "${TOKENS_DIR:-/data/tokens}" /data/staging /data/home
export XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# Xvfb + XFCE (escritorio para TightVNC)
Xvfb :10 -screen 0 1920x1080x24 -ac >/tmp/xvfb.log 2>&1 &
sleep 2
export DISPLAY=:10

if command -v dbus-launch >/dev/null 2>&1; then
    eval "$(dbus-launch --sh-syntax)" 2>/dev/null || true
fi

xfce4-session >/tmp/xfce.log 2>&1 &

if [ -f /data/vnc.pass ]; then
    x11vnc -display :10 -localhost -rfbport 5900 -forever -shared \
        -rfbauth /data/vnc.pass -o /tmp/x11vnc.log &
else
    # Sin fichero de password: solo accesible por localhost + túnel SSH.
    x11vnc -display :10 -localhost -rfbport 5900 -forever -shared \
        -nopw -o /tmp/x11vnc.log &
fi

# Agente en primer plano. El host publica 9101 solo en la IP de Tailscale.
exec python3 -m egress_agent \
    --config /data/agent_config.json \
    --host 0.0.0.0 \
    --port 9101
