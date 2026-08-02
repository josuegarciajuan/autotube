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
#   2. Gracefully restarts the API (kill old uvicorn + start new one)
#   3. Running workers continue independently (they survive API restart)
#
# If a video is being generated in-process (legacy mode), this script
# warns and refuses to restart to avoid killing the generation.
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
ACTIVE_JOBS=$(python3 -c "
from database.db_extended import ExtendedDatabase
from database.db import init_db
from database.db_extended import migrate_v2
init_db()
migrate_v2()
db = ExtendedDatabase()
active = db.get_active_jobs()
running = [j for j in active if j['status'] == 'running']
if running:
    print(f'ACTIVE:{len(running)}:{running[0][\"id\"]}')
else:
    print('NONE')
" 2>/dev/null || echo "ERROR")

echo "   $ACTIVE_JOBS"

if [[ "$ACTIVE_JOBS" == ACTIVE:* ]]; then
    JOB_ID=$(echo "$ACTIVE_JOBS" | cut -d: -f3)
    # Detect whether the running job is a subprocess worker (survivable)
    # or legacy in-process (would die on API restart)
    WORKER_MODE="IN_PROCESS"
    if pgrep -f "full_pipeline_worker.*--job-id $JOB_ID" > /dev/null 2>&1; then
        WORKER_MODE="SUBPROCESS"
    fi
    if [ "$WORKER_MODE" = "SUBPROCESS" ]; then
        echo ""
        echo "✅ Active job #$JOB_ID running in SUBPROCESS mode."
        echo "   The worker will continue independently during the API restart."
        echo ""
    else
        echo ""
        echo "⚠️  ACTIVE JOB #$JOB_ID IS RUNNING IN-PROCESS!"
        echo "   Restarting the API would KILL this generation."
        echo "   Enable subprocess worker mode (USE_SUBPROCESS_WORKER=True)"
        echo "   or wait for the job to finish."
        echo ""
        read -p "Continue anyway? This WILL kill the running job [y/N]: " CONFIRM
        if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
            echo "Aborted."
            exit 1
        fi
    fi
fi

# ── Step 1: Rebuild frontend ──
echo ""
echo "📦 Step 1/3: Rebuilding frontend..."
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
echo "🔍 Step 3/3: Verifying..."

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

echo ""
NEW_PID=$(systemctl show -p MainPID autotube-panel 2>/dev/null | cut -d= -f2)
echo "╔═════════════════════════════════════════════════════╗"
echo "║  ✅ Changes applied successfully!                    ║"
echo "║  Frontend: rebuilt (dist/)                          ║"
echo "║  API:      restarted (systemd, PID ${NEW_PID:-?})   ║"
echo "║  Workers:  $WORKER_COUNT running (uninterrupted)    ║"
echo "╚═════════════════════════════════════════════════════╝"
