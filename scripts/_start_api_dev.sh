#!/bin/bash
# Quick API launcher for dev mode with reload
cd /root/autotube
exec python3 -m uvicorn api.main:app \
    --host 0.0.0.0 --port 8000 \
    --reload \
    --reload-dir api \
    --reload-dir config \
    --reload-dir database \
    --reload-dir pipeline \
    --reload-exclude "__pycache__" \
    --log-level info \
    >> logs/api_dev.log 2>&1
