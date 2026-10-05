#!/usr/bin/env bash
# =============================================================================
# distribute_sd_runtime.sh — prepara un nodo para el worker de imágenes IA
# (Local SD 1.5) de autotube en el pool de SuperServer.
#
# Ejecutar desde un host con la imagen construida (`deploy/docker/build_sd.sh`)
# y acceso SSH a los nodos. Para cada nodo, en orden:
#   1. `docker save autotube-sd:1 | ssh <nodo> docker load` (imagen portable).
#   2. rsync del código a `/opt/taildeck/autotube` (sin secretos ni datos).
#   3. copia de la caché HF (SD 1.5) a `/opt/taildeck/autotube/.hfcache`.
#   4. smoke test: genera una imagen y exige `result.json` con ok=true.
#   5. (si el target se da como `nombre=ssh_target`) etiqueta el nodo con
#      `local-sd` vía la API del panel (loopback, sin cookies).
#
# Uso:
#   bash deploy/docker/distribute_sd_runtime.sh <casa> [nombre=]<nodo1> [...]
#   bash deploy/docker/distribute_sd_runtime.sh root@100.106.48.118 \
#        mail=root@100.77.212.12 dedi3133109=root@100.70.x.x
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "$0")/../.." && pwd)"
IMG="${AUTOTUBE_SD_IMAGE:-autotube-sd:1}"
DST="${AUTOTUBE_RUNTIME_DIR:-/opt/taildeck/autotube}"
SS_API="${AUTOTUBE_SS_API:-http://127.0.0.1:8110/superserver/api}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)

[ $# -ge 2 ] || { echo "uso: $0 <casa> [nombre=]<nodo1> [[nombre=]<nodo2> ...]" >&2; exit 2; }
CASA="$1"; shift

echo "==> imagen local $IMG"; docker image inspect "$IMG" --format '  size={{.Size}}'
echo "==> código: $DIR -> nodos:$DST (sin secrets/datos)"

# Etiqueta un nodo con `local-sd` fusionando las tags actuales (best-effort).
tag_node() {
    local name="$1"
    [ -n "$name" ] || return 0
    local csrf tags
    csrf=$(curl -s -H 'X-Panel-Gate: 1' "$SS_API/auth" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("csrf",""))' 2>/dev/null || true)
    [ -n "$csrf" ] || { echo "  aviso: sin CSRF; etiqueta $name manualmente (local-sd)"; return 0; }
    tags=$(curl -s -H 'X-Panel-Gate: 1' "$SS_API/state" | python3 -c "
import sys, json
name = sys.argv[1]
d = json.load(sys.stdin)
for n in d.get('nodes', []):
    if n.get('name') == name:
        t = list(n.get('tags') or [])
        if 'local-sd' not in t:
            t.append('local-sd')
        print(json.dumps(t)); break
else:
    print('[\"local-sd\"]')
" "$name" 2>/dev/null || echo '["local-sd"]')
    curl -s -X PATCH -H 'X-Panel-Gate: 1' -H "X-CSRF-Token: $csrf" \
        -H 'Content-Type: application/json' -d "{\"tags\": $tags}" \
        "$SS_API/nodes/$name" >/dev/null \
        && echo "  5/5 tag local-sd -> $name" \
        || echo "  aviso: no se pudo etiquetar $name (hazlo en el panel)"
}

for TARGET in "$@"; do
    NAME=""; HOST="$TARGET"
    case "$TARGET" in
        *=*) NAME="${TARGET%%=*}"; HOST="${TARGET#*=}" ;;
    esac
    echo "============================================================"
    echo "==> $HOST${NAME:+ (nodo $NAME)}"
    echo "  1/5 imagen"
    docker save "$IMG" | ssh "${SSH_OPTS[@]}" "$HOST" 'docker load'
    echo "  2/5 código"
    ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $DST"
    rsync -a --delete \
        --exclude '.git/' --exclude 'output/' --exclude 'logs/' --exclude 'frontend/' \
        --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.env' \
        --exclude 'tokens/' --exclude 'keys/' --exclude '*.db' --exclude '*.db-*' \
        --exclude '*.pickle' --exclude 'client_secret*' --exclude 'autotube.db*' \
        --exclude '.hfcache/' \
        -e "ssh ${SSH_OPTS[*]}" \
        "$DIR/" "$HOST:$DST/"
    echo "  3/5 cache SD 1.5 (~4 GB)"
    ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $DST/.hfcache/hub"
    rsync -a \
        -e "ssh ${SSH_OPTS[*]}" \
        "$HOME/.cache/huggingface/hub/models--runwayml--stable-diffusion-v1-5" \
        "$HOST:$DST/.hfcache/hub/"
    echo "  4/5 smoke test"
    ssh "${SSH_OPTS[@]}" "$HOST" "set -eu
        T=\$(mktemp -d /tmp/autotube-sd-smoke.XXXXXX); mkdir -p \$T/in \$T/out
        printf '%s' '{\"key\":\"smoke\",\"index\":0,\"prompt\":\"cinematic mountain landscape at sunset, 16:9\",\"negative_prompt\":\"blurry\",\"seed\":42,\"width\":384,\"height\":384,\"steps\":4,\"output\":\"smoke.jpg\",\"upscale_min\":null}' > \$T/in/request.json
        docker run --rm --network=none \
            -e HF_HOME=/hfcache -e HF_HUB_OFFLINE=1 -e PYTHONPATH=/app \
            -e OUTPUT_DIR=/tmp/worker_output -e SD_THREADS=1 -e OMP_NUM_THREADS=1 \
            -v $DST:/app:ro -v $DST/.hfcache:/hfcache:ro \
            -v \$T/in:/work/in:ro -v \$T/out:/work/out -w /work $IMG \
            python3 /app/pipeline_dist/image_worker.py \
              --request /work/in/request.json --out-dir /work/out >/dev/null 2>&1 || true
        if grep -q '\"ok\": true' \$T/out/result.json; then
            echo '  smoke: OK'
        else
            echo '  smoke: FALLO'; cat \$T/out/result.json 2>/dev/null; exit 1
        fi
        rm -rf \$T"
    tag_node "$NAME"
done

echo "==> listo"
