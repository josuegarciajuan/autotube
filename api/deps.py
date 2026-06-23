"""FastAPI dependencies."""
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.db_extended import ExtendedDatabase, migrate_v2
from database.db import init_db


# Singleton DB instance
_db_instance: ExtendedDatabase = None


def get_db() -> ExtendedDatabase:
    global _db_instance
    if _db_instance is None:
        init_db()
        migrate_v2()
        _db_instance = ExtendedDatabase()
    return _db_instance
