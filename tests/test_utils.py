"""Tests for shared API utilities."""

from datetime import datetime, timezone

from api import utils
from api.time_utils import local_to_utc, madrid_day_range, parse_utc, sqlite_utc, youtube_rfc3339


def test_db_now_is_utc_naive_sqlite_timestamp(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return datetime(2026, 8, 31, 12, 34, 56, tzinfo=timezone.utc)

    monkeypatch.setattr(utils, "datetime", FixedDateTime)

    assert utils.db_now() == "2026-08-31 12:34:56"


def test_time_contract_converts_madrid_and_utc_naive_values():
    assert local_to_utc("2026-08-30 23:59:00").isoformat() == "2026-08-30T21:59:00+00:00"
    assert parse_utc("2026-08-30T21:59:00+00:00").hour == 21
    assert sqlite_utc("2026-08-30T21:59:00+00:00") == "2026-08-30 21:59:00"
    assert youtube_rfc3339("2026-08-30 21:59:00") == "2026-08-30T21:59:00Z"


def test_madrid_day_range_handles_dst():
    start, end = madrid_day_range("2026-08-30")
    assert start == "2026-08-29 22:00:00"
    assert end == "2026-08-30 22:00:00"


def test_madrid_day_range_uses_next_local_midnight_in_winter():
    start, end = madrid_day_range("2026-10-25")
    assert start == "2026-10-24 22:00:00"
    assert end == "2026-10-25 23:00:00"


def test_timestamp_migration_dry_run_and_idempotency(tmp_path):
    from scripts.migrate_timestamps import migrate_timestamps
    import sqlite3
    db_path = tmp_path / "timestamps.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
          CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
          CREATE TABLE planned_slots (id INTEGER PRIMARY KEY, scheduled_at TEXT, target_upload_at TEXT, target_public_at TEXT);
          CREATE TABLE videos (id INTEGER PRIMARY KEY, target_public_at TEXT, scheduled_upload_at TEXT);
          CREATE TABLE shorts_planned_slots (id INTEGER PRIMARY KEY, scheduled_at TEXT, target_upload_at TEXT);
          INSERT INTO planned_slots VALUES (1, '2026-08-30 23:59:00', '2026-08-30 23:00:00', NULL);
        """)
    preview = migrate_timestamps(str(db_path), dry_run=True)
    assert preview["changed_count"] == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT scheduled_at FROM planned_slots").fetchone()[0] == "2026-08-30 23:59:00"
    applied = migrate_timestamps(str(db_path))
    assert applied["changed_count"] == 2
    again = migrate_timestamps(str(db_path))
    assert again.get("already_applied") is True
