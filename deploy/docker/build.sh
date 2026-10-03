#!/usr/bin/env bash
# Construye la imagen portable del worker TTS de autotube (SuperServer M4).
# Uso: bash deploy/docker/build.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
IMG="${AUTOTUBE_TTS_IMAGE:-autotube-tts:1}"

echo "==> construyendo $IMG"
docker build -t "$IMG" -f "$DIR/Dockerfile" "$DIR"
echo "==> imagen $IMG:"
docker image inspect "$IMG" --format '  size={{.Size}} bytes  id={{.Id}}'
