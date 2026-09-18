#!/bin/bash
# apply_changes.sh — Zero-downtime change application
#
# Rebuilds frontend and gracefully restarts the API without killing
# running video generation. Requires the subprocess worker to be enabled
# (USE_SUBPROCESS_WORKER=True, which is the default).
#
# Usage:
#   bash scripts/apply_changes.sh
#
# What it does:
#   1. Rebuilds the frontend (npm run build in frontend/)
#   2. Gracefully restarts the API (systemd, KillMode=process)
#   3. Running workers continue independently (they survive API restart)
#
# Deploy safety is decided by scripts/deploy_safety.py: it only blocks when a
# long-form generation runs IN-PROCESS (legacy mode, would die on restart).
# Subprocess workers are safe and the deploy proceeds. Aborting now exits 1
# (before it exited 0, so the post-merge auto-deploy failed silently).
# Kill-switch: SKIP_ACTIVE_WORKER_CHECK=true forces the deploy.
# Use start_dev.sh if you need hot-reload during development.

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "╔═════════════════════════════════════════════════════╗"
echo "║  Autotube — Zero-Downtime Change Application        ║"
echo "╚═════════════════════════════════════════════════════╝"
echo ""

# ── Python syntax check ── prevent IndentationError/syntax regressions
echo "🔍 Checking Python syntax..."
SYNTAX_ERRORS=0
for f in $(find pipeline api database config -name "*.py" -not -path "*/__pycache__/*" 2>/dev/null); do
    if ! python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null; then
        echo "   ❌ SYNTAX ERROR in $f"
        SYNTAX_ERRORS=$((SYNTAX_ERRORS + 1))
    fi
done
if [ $SYNTAX_ERRORS -gt 0 ]; then
    echo "❌ $SYNTAX_ERRORS file(s) with syntax errors — aborting deploy"
    exit 1
fi
echo "   ✅ All Python files pass syntax check"
echo ""

echo "🔍 Checking for active generation..."
# Decisión centralizada en scripts/deploy_safety.py (testeable, sin cuelgues de TTY):
#   - sin jobs long-form running         -> desplegar
#   - todos los jobs running son subprocess -> desplegar (KillMode=process)
#   - algún job running es in-process    -> abortar (exit 1) salvo force
if ! DEPLOY_CHECK_OUT=$(python3 scripts/deploy_safety.py 2>&1); then
    echo "   $DEPLOY_CHECK_OUT"
    echo ""
    echo "❌ Deploy bloqueado: hay generación IN-PROCESS activa que moriría al"
    echo "   reiniciar el API. Reintenta cuando termine, o fuerza con"
    echo "   SKIP_ACTIVE_WORKER_CHECK=true (solo si sabes lo que haces)."
    echo ""
    exit 1
fi
echo "   $DEPLOY_CHECK_OUT"

# ── Step 1: Rebuild frontend ──
echo ""
echo "📦 Step 1/3: Rebuilding frontend..."
# C5 (ago/sep 2026): los `vite build` se quedaban colgados en estado D
# (io_uring_del_tctx) y sobrevivían a kill -9, bloqueando el lock del hook
# post-merge. Deshabilitar io_uring en libuv/node evita el cuelgue.
export UV_USE_IO_URING=0
cd "$PROJECT_ROOT/frontend"
npm run build 2>&1 | tail -3
cd "$PROJECT_ROOT"
echo "   ✅ Frontend built"

# ── Step 2: Graceful API restart ──
echo ""
echo "🔄 Step 2/3: Restarting API server..."

# Use systemd to restart cleanly — avoids orphaned nohup processes
# holding port 8000 and causing restart storms.
echo "   Restarting via systemd (systemctl restart autotube-panel)..."
if ! systemctl restart autotube-panel 2>/dev/null; then
    echo "   ⚠️  systemd restart failed — manual fallback..."
    OLD_PID=$(pgrep -f "uvicorn api.main:app" 2>/dev/null || true)
    if [ -n "$OLD_PID" ]; then
        kill $OLD_PID 2>/dev/null || true
        # ── Wait for port release to prevent restart storm ──
        echo "   Waiting for port 8000 to be released..."
        for i in $(seq 1 15); do
            if ! ss -tlnp "sport = :8000" 2>/dev/null | grep -q ":8000"; then
                echo "   ✅ Port released after ${i}s"
                break
            fi
            sleep 1
        done
    fi
    systemctl start autotube-panel 2>/dev/null || true
fi
echo "   ✅ API restart triggered"

# ── Step 3: Verify ──
echo ""
echo "🔍 Step 3/4: Verifying..."

for i in $(seq 1 10); do
    if curl -s http://localhost:8000/api/stats > /dev/null 2>&1; then
        echo "   ✅ API is healthy"
        break
    fi
    sleep 1
done

# Check if any workers are still running
WORKER_COUNT=$(pgrep -f "full_pipeline_worker" 2>/dev/null | wc -l)
if [ "$WORKER_COUNT" -gt 0 ]; then
    echo "   ⏳ $WORKER_COUNT generation worker(s) still running (unaffected)"
fi

# ── Step 4: Remote agent sync (VPS de egreso) ──
# Cada despliegue actualiza también el agente egress del VPS (git pull + restart),
# para que el agente quede siempre en la misma versión que el server principal.
echo ""
echo "🔍 Step 4/4: Syncing egress agent on remote VPS..."
VPS_HOST=$(grep -E '^VPS_SSH_HOST=' .env 2>/dev/null | cut -d= -f2)
VPS_USER=$(grep -E '^VPS_SSH_USER=' .env 2>/dev/null | cut -d= -f2)
VPS_PASS=$(grep -E '^VPS_SSH_PASS=' .env 2>/dev/null | cut -d= -f2)
if [ -n "$VPS_HOST" ] && [ -n "$VPS_PASS" ]; then
    if command -v sshpass >/dev/null 2>&1; then
        VPS_SYNC=$(sshpass -p "$VPS_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
            "${VPS_USER:-root}@$VPS_HOST" \
            'cd /opt/autotube && git pull --ff-only origin main 2>&1 && systemctl restart egress-agent && echo VPS_SYNC_OK' 2>&1)
        if echo "$VPS_SYNC" | grep -q "VPS_SYNC_OK"; then
            echo "   ✅ Egress agent actualizado en $VPS_HOST (git pull + restart)"
        else
            echo "   ⚠️  No se pudo sincronizar el VPS ($VPS_HOST) — revisar SSH/red (no bloquea el deploy local)"
            echo "      $(echo "$VPS_SYNC" | tail -1)"
        fi
    else
        echo "   ⚠️  sshpass no instalado — no se pudo sincronizar el VPS"
    fi
else
    echo "   ⚪ VPS_SSH_HOST no configurado en .env — sin sincronización remota"
fi

echo ""
NEW_PID=$(systemctl show -p MainPID autotube-panel 2>/dev/null | cut -d= -f2)
echo "╔═════════════════════════════════════════════════════╗"
echo "║  ✅ Changes applied successfully!                    ║"
echo "║  Frontend: rebuilt (dist/)                          ║"
echo "║  API:      restarted (systemd, PID ${NEW_PID:-?})   ║"
echo "║  Workers:  $WORKER_COUNT running (uninterrupted)    ║"
echo "║  VPS:      ${VPS_SYNC:+sync attempted (see above)}  ║"
echo "╚═════════════════════════════════════════════════════╝"
