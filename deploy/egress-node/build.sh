#!/bin/sh
# Construye la imagen del gateway a partir del repo (solo ficheros versionados).
# Uso: IMAGE=autotube-egress:1 ./deploy/egress-node/build.sh
set -eu

ROOT=$(git rev-parse --show-toplevel)
IMAGE=${IMAGE:-autotube-egress:1}
CTX=$(mktemp -d /tmp/autotube-egress-build.XXXXXX)
trap 'rm -rf "$CTX"' EXIT

# Contexto limpio: sin secretos, DB, tokens, perfiles ni artefactos.
git -C "$ROOT" archive HEAD | tar -x -C "$CTX"
cat > "$CTX/.dockerignore" <<'EOF'
.git
frontend
assets
backups
reports
tests
output
logs
tokens
data
*.db
*.db-*
.env
EOF

echo "🔨 Construyendo $IMAGE desde $CTX"
docker build -f "$CTX/deploy/egress-node/Dockerfile" -t "$IMAGE" "$CTX"
echo "✅ Imagen lista: $IMAGE"
