#!/usr/bin/env bash
# =============================================================================
# distribute_render_runtime.sh — prepara nodos para el render de escena de
# Autotube en el pool de SuperServer.
#
# Por cada nodo:
#   1. `docker save autotube-render:1 | ssh <nodo> docker load`
#   2. smoke: renderiza una escena de prueba (kind=test) sin red y exige
#      result.json con ok=true y el MP4 resultante.
#
# Uso:
#   bash deploy/docker/distribute_render_runtime.sh <nodo1> [nodo2 ...]
#   bash deploy/docker/distribute_render_runtime.sh root@100.106.48.118
# =============================================================================
set -euo pipefail

IMG="${AUTOTUBE_RENDER_IMAGE:-autotube-render:1}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)

[ $# -ge 1 ] || { echo "uso: $0 <nodo1> [nodo2 ...]" >&2; exit 2; }

for NODE in "$@"; do
    echo "============================================================"
    echo "==> $NODE"
    echo "  1/2 imagen"
    docker save "$IMG" | ssh "${SSH_OPTS[@]}" "$NODE" 'docker load'
    echo "  2/2 smoke (render de escena de prueba, sin red)"
    ssh "${SSH_OPTS[@]}" "$NODE" "set -eu
        T=\$(mktemp -d /tmp/autotube-render.XXXXXX); mkdir -p \$T/in \$T/out
        cat > \$T/in/manifest.json <<'JSON'
{\"schema_version\":1,\"scene_key\":\"smoke\",\"index\":0,\"frames\":30,\"fps\":30.0,\"width\":320,\"height\":180,\"duration\":1.0,\"asset\":{\"kind\":\"test\",\"path\":\"\",\"sha256\":\"\",\"trim_start\":0.0},\"motion\":{\"zoom_start\":1.0,\"zoom_end\":1.0,\"focus\":\"center\"},\"color\":{\"contrast\":1.0,\"brightness\":1.0,\"saturation\":1.0},\"encoding\":{\"codec\":\"libx264\",\"crf\":16,\"preset\":\"veryfast\",\"pix_fmt\":\"yuv420p\"}}
JSON
        docker run --rm --network=none -v \$T/in:/work/in:ro -v \$T/out:/work/out -w /work $IMG \
            python /app/pipeline_dist/scene_worker.py --manifest /work/in/manifest.json --out-dir /work/out >/dev/null 2>&1 || true
        if [ -s \$T/out/result.json ] && [ -s \$T/out/scene.mp4 ]; then
            echo '  smoke: OK'; else echo '  smoke: FALLO'; exit 1; fi
        rm -rf \$T"
done

echo "==> listo"
