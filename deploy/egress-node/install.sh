#!/bin/sh
# Instala un gateway egress por canal en un nodo (contenedor + systemd + VNC).
#
# Uso (root, en el nodo):
#   ./install.sh --slug canal6 --agent-port 9106 --vnc-port 5956 \
#                --expected-ip 151.80.142.93 --google-account cuenta@gmail.com
#
# Requisitos: docker, systemd, openssl. La imagen debe existir en el nodo
# (ver deploy/egress-node/README.md → build/transfer).
set -eu

SLUG=""
AGENT_PORT="9101"
VNC_PORT="5950"
BIND_IP="auto"
EXPECTED_IP=""
GOOGLE_ACCOUNT=""
EGRESS_LABEL=""
IMAGE="${IMAGE:-autotube-egress:1}"
BASE_DIR="${BASE_DIR:-/opt/autotube-egress}"

while [ $# -gt 0 ]; do
  case "$1" in
    --slug) SLUG=${2:-}; shift 2 ;;
    --agent-port) AGENT_PORT=${2:-}; shift 2 ;;
    --vnc-port) VNC_PORT=${2:-}; shift 2 ;;
    --bind-ip) BIND_IP=${2:-}; shift 2 ;;
    --expected-ip) EXPECTED_IP=${2:-}; shift 2 ;;
    --google-account) GOOGLE_ACCOUNT=${2:-}; shift 2 ;;
    --egress-label) EGRESS_LABEL=${2:-}; shift 2 ;;
    --image) IMAGE=${2:-}; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Argumento desconocido: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Ejecutar como root" >&2; exit 1; }
case "$SLUG" in
  ""|*[!a-z0-9_-]*) echo "--slug obligatorio (minúsculas, números, _ -)" >&2; exit 2 ;;
esac

command -v docker >/dev/null 2>&1 || { echo "docker no instalado" >&2; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "systemd no disponible" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "openssl no instalado" >&2; exit 1; }

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "La imagen '$IMAGE' no existe en este nodo. Construir/transferir primero." >&2
  exit 1
}

if [ "$BIND_IP" = "auto" ]; then
  BIND_IP=$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -n1 || true)
  [ -n "$BIND_IP" ] || BIND_IP="127.0.0.1"
fi
if [ -z "$EXPECTED_IP" ]; then
  EXPECTED_IP=$(curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
fi
[ -n "$EXPECTED_IP" ] || {
  echo "No se pudo detectar la IP de egress. Pasa --expected-ip." >&2
  exit 1
}
[ -n "$EGRESS_LABEL" ] || EGRESS_LABEL="$(hostname)-$EXPECTED_IP"

FREE_GB=$(df -Pk / | awk 'NR==2 {printf "%.0f", $4/1024/1024}')
if [ "$FREE_GB" -lt 8 ]; then
  echo "⚠️  Solo ${FREE_GB} GB libres en /. Se recomiendan >= 8 GB (staging + perfiles)." >&2
fi

DIR="$BASE_DIR/$SLUG"
mkdir -p "$DIR/tokens" "$DIR/staging" "$DIR/home"
chmod 700 "$DIR"
chown -R 1000:1000 "$DIR"

# Token del agente (se copia al registro del servidor principal).
AGENT_TOKEN=$(openssl rand -hex 32)
# Password VNC (se usa por túnel SSH; nunca se expone el puerto a Internet).
VNC_PASS=$(openssl rand -base64 16 | tr -d '/+=' | cut -c1-12)

docker run --rm --entrypoint sh -v "$DIR:/data" "$IMAGE" \
  -c "x11vnc -storepasswd '$VNC_PASS' /data/vnc.pass" >/dev/null 2>&1 || {
    echo "No se pudo generar /data/vnc.pass" >&2; exit 1;
  }

cat > "$DIR/agent_config.json" <<EOF
{
  "slug": "$SLUG",
  "google_account": "$GOOGLE_ACCOUNT",
  "egress_label": "$EGRESS_LABEL",
  "auth_token": "$AGENT_TOKEN",
  "expected_ip": "$EXPECTED_IP",
  "egress_verify": true,
  "project_root": "/app",
  "client_secret_path": "/data/client_secret_$SLUG.json",
  "token_pickle_path": "/data/tokens/$SLUG.pickle",
  "browser_profile_dir": "/data/tokens/${GOOGLE_ACCOUNT:-$SLUG}_browser_profile",
  "browser_session_path": "/data/tokens/${GOOGLE_ACCOUNT:-$SLUG}_browser_session.json",
  "host": "0.0.0.0",
  "port": 9101,
  "max_body_mb": 4096,
  "staging_dir": "/data/staging",
  "staging_max_gb": 5,
  "staging_ttl_hours": 6,
  "min_free_gb": 2,
  "playwright_proxy": {}
}
EOF
chmod 600 "$DIR/agent_config.json" "$DIR/vnc.pass"
chown 1000:1000 "$DIR/agent_config.json" "$DIR/vnc.pass"

UNIT="/etc/systemd/system/egress-$SLUG.service"
cat > "$UNIT" <<EOF
[Unit]
Description=Autotube egress gateway ($SLUG)
After=docker.service network-online.target
Requires=docker.service

[Service]
Restart=always
RestartSec=10
TimeoutStopSec=30
ExecStartPre=-/usr/bin/docker rm -f egress-$SLUG
ExecStart=/usr/bin/docker run --rm --name egress-$SLUG --user 1000:1000 --shm-size=1g \\
  -p $BIND_IP:$AGENT_PORT:9101 -p 127.0.0.1:$VNC_PORT:5900 \\
  -v $DIR:/data \\
  -e TZ=Europe/Madrid -e SLUG=$SLUG -e GOOGLE_ACCOUNT=$GOOGLE_ACCOUNT \\
  $IMAGE
ExecStop=/usr/bin/docker stop -t 20 egress-$SLUG

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "egress-$SLUG.service"
systemctl restart "egress-$SLUG.service"
sleep 4

CHECK_HOST="$BIND_IP"
[ "$CHECK_HOST" = "0.0.0.0" ] && CHECK_HOST="127.0.0.1"
echo ""
echo "✅ Gateway '$SLUG' instalado y arrancado."
echo "   Agente:  http://$BIND_IP:$AGENT_PORT  (token en el snippet de abajo)"
echo "   IP esperada: $EXPECTED_IP"
echo "   Datos:   $DIR"
echo ""
echo "── Copia esta entrada en config/egress_agents.json del servidor principal ──"
cat <<EOF
  "$SLUG": {
    "url": "http://$BIND_IP:$AGENT_PORT",
    "token": "$AGENT_TOKEN",
    "expected_ip": "$EXPECTED_IP",
    "egress_label": "$EGRESS_LABEL"
  }
EOF
echo ""
echo "── TightVNC (desde tu máquina; túnel SSH) ──"
echo "   ssh -L $VNC_PORT:127.0.0.1:$VNC_PORT root@$(hostname -I | awk '{print $1}')"
echo "   TightVNC Viewer → localhost:$VNC_PORT   (password: $VNC_PASS)"
echo "   Dentro del escritorio: /app/deploy/egress-node/navegador.sh"
echo ""
echo "── Comprobación local ──"
echo "   curl -s http://$CHECK_HOST:$AGENT_PORT/ping"
echo "   curl -s http://$CHECK_HOST:$AGENT_PORT/healthz"
