#!/bin/bash
# stop_dev.sh — Stop the Autotube development servers
# 
# Kills both the API (uvicorn) and Vite dev server.
# Does NOT kill running generation subprocesses (workers survive).

echo "🛑 Stopping development servers..."

# Kill API
if [ -f /tmp/autotube_dev_api.pid ]; then
    PID=$(cat /tmp/autotube_dev_api.pid)
    kill $PID 2>/dev/null && echo "   API (PID $PID) stopped" || true
    rm -f /tmp/autotube_dev_api.pid
else
    pkill -f "uvicorn api.main" 2>/dev/null && echo "   API stopped" || true
fi

# Kill Vite
if [ -f /tmp/autotube_dev_vite.pid ]; then
    PID=$(cat /tmp/autotube_dev_vite.pid)
    kill $PID 2>/dev/null && echo "   Vite (PID $PID) stopped" || true
    rm -f /tmp/autotube_dev_vite.pid
else
    pkill -f "vite" 2>/dev/null && echo "   Vite stopped" || true
fi

echo ""
echo "ℹ️  Running generation workers (if any) are NOT affected."
echo "   They run as independent processes with start_new_session."

# Check for active workers
ACTIVE_WORKERS=$(pgrep -f "full_pipeline_worker" 2>/dev/null | wc -l)
if [ "$ACTIVE_WORKERS" -gt 0 ]; then
    echo "   ⏳ $ACTIVE_WORKERS worker(s) still running (safe)."
fi
