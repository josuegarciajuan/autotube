#!/bin/bash
# start_dev.sh — Autotube Development Server
# 
# Starts both the API (with hot-reload) and Vite frontend (with HMR)
# in development mode. Changes to Python files and frontend source
# are reflected instantly WITHOUT restarting the server.
#
# KEY DIFFERENCE from production:
#   - Video generation runs as a SEPARATE INDEPENDENT PROCESS
#     (full_pipeline_worker.py via subprocess with start_new_session)
#   - Restarting the API does NOT kill running video generation
#   - Frontend changes appear instantly via Vite HMR
#
# Usage:
#   bash scripts/start_dev.sh
#
# Access:
#   Frontend: http://localhost:5173 (Vite HMR)
#   API only:  http://localhost:8000/api/docs
#   Logs:      tail -f logs/api.log
#
# Stop: Ctrl+C kills both servers

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║     Autotube Development Server                          ║"
echo "║     Hot Reload: API ✅  |  Frontend ✅ (HMR)            ║"
echo "║     Generation: Worker subprocess (safe to restart API) ║"
echo "╚══════════════════════════════════════════════════════════╝"
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
    echo "❌ $SYNTAX_ERRORS file(s) with syntax errors — aborting start"
    exit 1
fi
echo "   ✅ All Python files pass syntax check"

echo ""
echo "🔍 Checking for active generation..."
ACTIVE=$(python3 -c "
from database.db_extended import ExtendedDatabase
from database.db import init_db
from database.db_extended import migrate_v2
init_db()
migrate_v2()
db = ExtendedDatabase()
job = db.get_active_job()
if job:
    print(f'WARNING: Active job #{job[\"id\"]} is {job[\"status\"]}')
else:
    print('OK')
" 2>/dev/null || echo "WARN (DB check failed)")
echo "   $ACTIVE"
echo ""

# ── Kill existing uvicorn and vite processes ──
echo "🧹 Cleaning up old processes..."
pkill -f "uvicorn api.main" 2>/dev/null || true
pkill -f "vite" 2>/dev/null || true

# Wait for port 8000 to be released BEFORE starting new uvicorn.
# Without this, uvicorn crashes with [Errno 98] Address already in use,
# which triggers StatReload to keep trying → restart storm → workers killed.
echo "   Waiting for port 8000 to be released..."
for i in $(seq 1 10); do
    if ! ss -tlnp "sport = :8000" 2>/dev/null | grep -q ":8000"; then
        echo "   ✅ Port 8000 released after ${i}s"
        break
    fi
    sleep 1
done
if ss -tlnp "sport = :8000" 2>/dev/null | grep -q ":8000"; then
    echo "   ⚠️  Port 8000 still in use after 10s — forcing with fuser"
    fuser -k 8000/tcp 2>/dev/null || true
    sleep 2
fi

# ── Ensure subprocess worker mode is ON ──
# (Already default: USE_SUBPROCESS_WORKER = True in generation_service.py)
echo "✅ Subprocess worker mode: ON (generation survives API restarts)"

# ── Start API with hot-reload ──
echo ""
echo "🚀 Starting API server (port 8000, auto-reload on .py changes)..."
nohup python3 -m uvicorn api.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --reload-dir api \
    --reload-dir config \
    --reload-dir database \
    --reload-dir pipeline \
    --reload-dir orchestrator.py \
    --reload-exclude "**/__pycache__/**" \
    --reload-exclude "**/*.log" \
    --reload-exclude "*.pyc" \
    --log-level info \
    > logs/api_dev.log 2>&1 &

API_PID=$!
echo "   API PID: $API_PID (logs: logs/api_dev.log)"

# ── Wait for API to be ready ──
echo "   Waiting for API to start..."
for i in $(seq 1 15); do
    if curl -s http://localhost:8000/api/stats > /dev/null 2>&1; then
        echo "   ✅ API ready"
        break
    fi
    sleep 1
done

# ── Start Vite dev server with HMR ──
echo ""
echo "🎨 Starting Vite dev server (port 5173, Hot Module Replacement)..."
cd "$PROJECT_ROOT/frontend"
nohup npm run dev -- --host 0.0.0.0 > ../logs/vite_dev.log 2>&1 &
VITE_PID=$!
cd "$PROJECT_ROOT"

echo "   Vite PID: $VITE_PID (logs: logs/vite_dev.log)"

# ── Wait for Vite ──
echo "   Waiting for Vite to start..."
for i in $(seq 1 10); do
    if curl -s http://localhost:5173 > /dev/null 2>&1; then
        echo "   ✅ Vite ready"
        break
    fi
    sleep 1
done

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  🟢 Development server is RUNNING                        ║"
echo "║                                                          ║"
echo "║  Frontend (HMR): http://localhost:5173                   ║"
echo "║  API Swagger:    http://localhost:8000/api/docs           ║"
echo "║                                                          ║"
echo "║  Stop:            bash scripts/stop_dev.sh               ║"
echo "║  API logs:        tail -f logs/api_dev.log               ║"
echo "║  Vite logs:       tail -f logs/vite_dev.log              ║"
echo "║  Worker logs:     tail -f logs/worker_*.log              ║"
echo "╚══════════════════════════════════════════════════════════╝"

# ── Save PIDs for stop script ──
echo "$API_PID" > /tmp/autotube_dev_api.pid
echo "$VITE_PID" > /tmp/autotube_dev_vite.pid
