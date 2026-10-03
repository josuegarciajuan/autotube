#!/usr/bin/env bash
# Construye la imagen del worker de render (contexto = raíz del repo).
set -euo pipefail
DIR="$(cd "$(dirname "$0")/../.." && pwd)"
IMG="${AUTOTUBE_RENDER_IMAGE:-autotube-render:1}"
echo "==> docker build $IMG"
docker build -f "$DIR/deploy/docker/Dockerfile.render" -t "$IMG" "$DIR"
docker image inspect "$IMG" --format '  size={{.Size}}'
