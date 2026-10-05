#!/usr/bin/env bash
# Construye la imagen portable del worker de imágenes IA (Local SD 1.5) de
# autotube para el pool de SuperServer.
# Uso: bash deploy/docker/build_sd.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMG="${AUTOTUBE_SD_IMAGE:-autotube-sd:1}"

echo "==> construyendo $IMG"
docker build -t "$IMG" -f "$ROOT/deploy/docker/Dockerfile.sd" "$ROOT"
echo "==> imagen $IMG:"
docker image inspect "$IMG" --format '  size={{.Size}} bytes  id={{.Id}}'
echo "==> comprobación de imports:"
docker run --rm "$IMG" python3 -c \
  'import torch, diffusers, cv2; print("torch", torch.__version__, "| diffusers", diffusers.__version__, "| cv2", cv2.__version__)'
