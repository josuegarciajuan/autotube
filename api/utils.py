"""
Shared utilities for the Autotube API.
"""

from datetime import datetime, timezone

from api.time_utils import sqlite_utc


def db_now() -> str:
    """Return current UTC timestamp in SQLite-compatible format.

    Matches SQLite CURRENT_TIMESTAMP format: YYYY-MM-DD HH:MM:SS
    The value is deliberately UTC-naive because SQLite CURRENT_TIMESTAMP and
    existing database columns use a timestamp without a timezone marker. The
    frontend treats such values as UTC before converting them for display.
    """
    return sqlite_utc(datetime.now(timezone.utc))
