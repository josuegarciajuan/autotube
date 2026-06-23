#!/bin/bash
# Autotube v2 Panel — Start script
# Serves both the API and the React SPA on port 8000
# Access at: http://localhost:8000 or http://lamami.online:8000

set -e

cd "$(dirname "$0")"

# Ensure DB is migrated
python3 -c "from database.db import init_db; from database.db_extended import migrate_v2; init_db(); migrate_v2(); print('DB OK')"

# Ensure frontend is built
if [ ! -f frontend/dist/index.html ]; then
    echo "Building frontend..."
    cd frontend && npm run build && cd ..
fi

# Start server
echo "Starting Autotube Panel on http://0.0.0.0:8000"
exec python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --log-level info
