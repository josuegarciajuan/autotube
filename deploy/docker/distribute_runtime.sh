#!/usr/bin/env bash
# =============================================================================
# distribute_runtime.sh — prepara un nodo para el worker TTS de autotube en el
# pool de SuperServer (M4).
#
# Ejecutar desde un host con la imagen construida (`deploy/docker/build.sh`) y
# acceso SSH a los nodos. Para cada nodo, en orden:
#   1. `docker save autotube-tts:1 | ssh <nodo> docker load` (imagen portable).
#   2. rsync del código a `/opt/taildeck/autotube` (sin secretos ni datos).
#   3. copia de la cache HF (Kokoro-82M) a `/opt/taildeck/autotube/.hfcache`.
#   4. smoke test: sintetiza un bloque y exige `estado.json` con ok=true.
#
# Uso:
#   bash deploy/docker/distribute_runtime.sh <casa> <nodo1> [nodo2 ...]
#   bash deploy/docker/distribute_runtime.sh root@100.106.48.118 root@100.77.212.12
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "$0")/../.." && pwd)"
IMG="${AUTOTUBE_TTS_IMAGE:-autotube-tts:1}"
DST="${AUTOTUBE_RUNTIME_DIR:-/opt/taildeck/autotube}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)

[ $# -ge 2 ] || { echo "uso: $0 <casa> <nodo1> [nodo2 ...]" >&2; exit 2; }
CASA="$1"; shift

echo "==> imagen local $IMG"; docker image inspect "$IMG" --format '  size={{.Size}}'
echo "==> código: $DIR -> nodos:$DST (sin secrets/datos)"

for NODE in "$@"; do
    echo "============================================================"
    echo "==> $NODE"
    echo "  1/4 imagen"
    docker save "$IMG" | ssh "${SSH_OPTS[@]}" "$NODE" 'docker load'
    echo "  2/4 código"
    ssh "${SSH_OPTS[@]}" "$NODE" "mkdir -p $DST"
    rsync -a --delete \
        --exclude '.git/' --exclude 'output/' --exclude 'logs/' --exclude 'frontend/' \
        --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.env' \
        --exclude 'tokens/' --exclude 'keys/' --exclude '*.db' --exclude '*.db-*' \
        --exclude '*.pickle' --exclude 'client_secret*' --exclude 'autotube.db*' \
        --exclude '.hfcache/' \
        -e "ssh ${SSH_OPTS[*]}" \
        "$DIR/" "$NODE:$DST/"
    echo "  3/4 cache Kokoro-82M"
    ssh "${SSH_OPTS[@]}" "$NODE" "mkdir -p $DST/.hfcache/hub"
    rsync -a \
        -e "ssh ${SSH_OPTS[*]}" \
        "$HOME/.cache/huggingface/hub/models--hexgrad--Kokoro-82M" \
        "$NODE:$DST/.hfcache/hub/"
    echo "  4/4 smoke test"
    ssh "${SSH_OPTS[@]}" "$NODE" "set -eu
        T=\$(mktemp -d /tmp/autotube-smoke.XXXXXX); mkdir -p \$T/in \$T/out
        printf '%s' '{\"bloques\":[{\"tipo\":\"hook\",\"texto\":\"Prueba de sintesis.\"}],\"voice_config\":{\"kokoro_voice\":\"em_santa\"},\"output_base\":\"narration_smoke\",\"rid\":\"smoke\"}' > \$T/in/request.json
        docker run --rm --network=none -e HF_HUB_OFFLINE=1 -e OMP_NUM_THREADS=1 \
            -e OPENBLAS_NUM_THREADS=1 -e MKL_NUM_THREADS=1 -e KOKORO_INLINE=1 \
            -e KOKORO_MP3_SNDFILE=1 \
            -v $DST:/app:ro -v $DST/.hfcache:/hfcache:ro -v \$T:/work -w /work $IMG \
            python /app/api/services/tts_worker.py --request /work/in/request.json --out-dir /work/out >/dev/null 2>&1 || true
        grep -q '\"ok\": true' \$T/out/estado.json && echo '  smoke: OK' || { echo '  smoke: FALLO'; cat \$T/out/estado.json 2>/dev/null; exit 1; }
        rm -rf \$T"
done

echo "==> listo"
