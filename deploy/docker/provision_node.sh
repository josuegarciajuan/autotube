#!/usr/bin/env bash
# =============================================================================
# provision_node.sh — prepara un nodo para ejecutar tareas distribuidas de
# Autotube (tier `render`) y lo etiqueta en SuperServer si pasa el smoke.
#
# Por cada nodo:
#   1. `docker save autotube-render:1 | ssh <nodo> docker load`
#   2. smoke con ASSET REAL (imagen) usando los mismos flags que la definición
#      (`--security-opt seccomp=unconfined`, `--cpus` solo si hay cgroup cpu).
#   3. alta del tag del tier vía API de SuperServer (auto-etiquetado).
#
# El smoke falla si no se produce result.json + scene.mp4; en ese caso NO se
# etiqueta el nodo (nunca se le manda trabajo que no puede ejecutar).
#
# Uso:
#   bash deploy/docker/provision_node.sh <node_name> <ssh_target> [tier]
#   bash deploy/docker/provision_node.sh oficina root@100.117.92.74 render
#
# Entorno: SUPERSERVER_API (def. http://localhost:8110/superserver),
#          SUPERSERVER_STATE (def. /root/superserver/data/state.json),
#          AUTOTUBE_RENDER_IMAGE (def. autotube-render:1)
# =============================================================================
set -euo pipefail

[ $# -ge 2 ] || { echo "uso: $0 <node_name> <ssh_target> [tier]" >&2; exit 2; }
NODE="$1"
TARGET="$2"
TIER="${3:-render}"
IMG="${AUTOTUBE_RENDER_IMAGE:-autotube-render:1}"
API="${SUPERSERVER_API:-http://localhost:8110/superserver}"
STATE="${SUPERSERVER_STATE:-/root/superserver/data/state.json}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new)

command -v docker >/dev/null || { echo "docker no disponible en el control plane"; exit 2; }
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "imagen $IMG no construida"; exit 2; }

echo "============================================================"
echo "==> $NODE ($TARGET) tier=$TIER"
echo "  1/3 imagen"
docker save "$IMG" | ssh "${SSH_OPTS[@]}" "$TARGET" 'docker load' >/tmp/atube_prov_load.log 2>&1 \
    || { tail -3 /tmp/atube_prov_load.log; exit 1; }

echo "  2/3 smoke con asset real"
TMP=$(mktemp -d /tmp/atube-prov.XXXXXX)
ffmpeg -y -v error -f lavfi -i "testsrc=size=640x360:duration=0.1:rate=30" -frames:v 1 "$TMP/asset.jpg"
scp -q "${SSH_OPTS[@]}" "$TMP/asset.jpg" "$TARGET:/tmp/atube_prov_asset.jpg"
ssh "${SSH_OPTS[@]}" "$TARGET" "set -eu
    D=\$(mktemp -d /tmp/atube-render.XXXXXX); mkdir -p \$D/in \$D/out
    cp /tmp/atube_prov_asset.jpg \$D/in/asset.jpg
    cat > \$D/in/manifest.json <<'JSON'
{\"schema_version\":1,\"scene_key\":\"smoke\",\"index\":0,\"frames\":30,\"fps\":30,\"width\":320,\"height\":180,\"duration\":1.0,\"asset\":{\"kind\":\"image\",\"path\":\"asset.jpg\",\"sha256\":\"\",\"trim_start\":0.0},\"motion\":{\"zoom_start\":1.0,\"zoom_end\":1.05,\"focus\":\"center\"},\"color\":{\"contrast\":1.0,\"brightness\":1.0,\"saturation\":1.0},\"encoding\":{\"codec\":\"libx264\",\"crf\":16,\"preset\":\"veryfast\",\"pix_fmt\":\"yuv420p\"}}
JSON
    CPUFLAG=''; for P in /sys/fs/cgroup/cpu/cpu.cfs_quota_us /sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us; do [ -e \"\$P\" ] && CPUFLAG='--cpus=1' && break; done
    docker run --rm --network=none --security-opt seccomp=unconfined \$CPUFLAG -v \$D/in:/work/in:ro -v \$D/out:/work/out -w /work $IMG python /app/pipeline_dist/scene_worker.py --manifest /work/in/manifest.json --out-dir /work/out >/dev/null 2>&1 || true
    test -s \$D/out/result.json && test -s \$D/out/scene.mp4 && echo '  smoke: OK' || { echo '  smoke: FALLO'; exit 1; }
    rm -rf \$D /tmp/atube_prov_asset.jpg"
rm -rf "$TMP"

echo "  3/3 etiquetar '$TIER'"
TOKEN=$(curl -s -H 'X-Panel-Gate: 1' "$API/api/auth" | python3 -c 'import sys,json;print(json.load(sys.stdin)["csrf"])')
NEWTAGS=$(python3 - "$STATE" "$NODE" "$TIER" <<'PY'
import json, sys
state, name, tier = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    st = json.load(open(state, encoding="utf-8"))
    tags = list(((st.get("nodes", {}) or {}).get(name, {}) or {}).get("tags") or [])
except Exception:
    tags = []
if tier not in tags:
    tags.append(tier)
print(json.dumps(tags))
PY
)
NODE_ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$NODE")
curl -s -X PUT -H 'X-Panel-Gate: 1' -H "X-CSRF-Token: $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"tags\":$NEWTAGS}" "$API/api/nodes/$NODE_ENC" >/dev/null
echo "  OK: $NODE etiquetado $NEWTAGS"
