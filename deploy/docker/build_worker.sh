#!/bin/sh
# Construye la imagen hermética autotube-worker:1 (toolchain pineado).
# Requisito: reproducir bit-a-bit el resultado del control plane.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMG="${AUTOTUBE_WORKER_IMAGE:-autotube-worker:1}"

echo "🔨 Building $IMG ..."
docker build -t "$IMG" -f "$ROOT/deploy/docker/Dockerfile.worker" "$ROOT"

echo "🔎 Verificando toolchain:"
docker run --rm "$IMG" sh -c 'ffmpeg -version | head -1'
docker run --rm "$IMG" python3 -c \
  'import imageio_ffmpeg, moviepy, imageio; print("imageio-ffmpeg", imageio_ffmpeg.__version__, "| moviepy", moviepy.__version__, "| imageio", imageio.__version__)'
echo "✅ $IMG lista."
