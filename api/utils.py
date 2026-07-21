"""
Shared utilities for the Autotube API.
"""

from datetime import datetime


def db_now() -> str:
    """Return current local timestamp in DB-compatible format.

    Matches SQLite CURRENT_TIMESTAMP format: YYYY-MM-DD HH:MM:SS
    All DB timestamps are stored in server local time (Europe/Madrid).
    """
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
