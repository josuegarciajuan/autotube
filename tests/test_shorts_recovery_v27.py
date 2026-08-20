"""Tests for v27 shorts recovery planner fixes.

Covers:
- _local_hm_to_utc: recovery slots must be stored in UTC (dispatcher treats
  scheduled_at as UTC; storing Madrid wall-clock fired them ~2h late).
- _channel_shorts_spam_blocked: spam-blocked channels are skipped by recovery.
- _count_clip_coverage: v26-aware coverage counts pre-rendered 'ready' shorts
  and pending/running slots (any date) so recovery stops fabricating doomed
  duplicate clip slots.
"""

import sqlite3
import time

import pytest

from api.services.shorts_recovery_planner import (
    _channel_shorts_spam_blocked,
    _count_clip_coverage,
    _local_hm_to_utc,
)
from database.db_extended import _migrate_v42


class _FakeDB:
    """Minimal db wrapper exposing _connect() to a sqlite in-memory DB."""

    def __init__(self, conn):
        self._conn = conn

    def _connect(self):
        return self._conn

    def get_system_state(self, key):
        row = self._conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE shorts (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            source_video_id INTEGER,
            type TEXT,
            status TEXT,
            title TEXT
        );
        CREATE TABLE shorts_planned_slots (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            date_key TEXT,
            scheduled_at TEXT,
            target_upload_at TEXT,
            short_type TEXT,
            long_slot_position INTEGER,
            source_video_id INTEGER,
            short_id INTEGER,
            status TEXT,
            slot_position INTEGER
        );
        """
    )
    yield _FakeDB(conn)
    conn.close()


@pytest.fixture
def mig_conn():
    """In-memory DB with the full schema subset needed by _migrate_v42."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY,
            video_path TEXT,
            status TEXT
        );
        CREATE TABLE shorts (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            source_video_id INTEGER,
            type TEXT,
            status TEXT,
            title TEXT
        );
        CREATE TABLE shorts_planned_slots (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            date_key TEXT,
            scheduled_at TEXT,
            target_upload_at TEXT,
            short_type TEXT,
            long_slot_position INTEGER,
            source_video_id INTEGER,
            short_id INTEGER,
            status TEXT,
            slot_position INTEGER,
            error_message TEXT,
            updated_at TEXT
        );
        """
    )
    yield conn
    conn.close()


def _insert_short(conn, short_id, channel_id, source_video_id, status,
                  short_type="clip"):
    conn.execute(
        """INSERT INTO shorts (id, channel_id, source_video_id, type, status, title)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (short_id, channel_id, source_video_id, short_type, status, f"t{short_id}"),
    )


def _insert_slot(conn, slot_id, channel_id, source_video_id, status,
                 short_id=None, date_key="2026-08-21"):
    conn.execute(
        """INSERT INTO shorts_planned_slots
           (id, channel_id, date_key, scheduled_at, target_upload_at,
            short_type, long_slot_position, source_video_id, short_id, status)
           VALUES (?, ?, ?, ?, ?, 'clip', 1, ?, ?, ?)""",
        (slot_id, channel_id, date_key, f"{date_key} 10:00:00",
         f"{date_key} 10:30:00", source_video_id, short_id, status),
    )


# ── _local_hm_to_utc ──────────────────────────────────────────────

def test_local_hm_to_utc_summer():
    # Europe/Madrid in August is UTC+2
    assert _local_hm_to_utc("2026-08-20", 15, 0) == "2026-08-20 13:00:00"
    assert _local_hm_to_utc("2026-08-20", 17, 13) == "2026-08-20 15:13:00"


def test_local_hm_to_utc_winter():
    # January is UTC+1
    assert _local_hm_to_utc("2026-01-10", 15, 0) == "2026-01-10 14:00:00"


# ── _channel_shorts_spam_blocked ─────────────────────────────────

def test_spam_blocked_not_set(db):
    assert _channel_shorts_spam_blocked(7, db) is False


def test_spam_blocked_future(db):
    db._conn.execute(
        "INSERT INTO system_state VALUES (?, ?)",
        (f"shorts_spam_blocked_until_7", str(time.time() + 3600)),
    )
    db._conn.commit()
    assert _channel_shorts_spam_blocked(7, db) is True


def test_spam_blocked_expired(db):
    db._conn.execute(
        "INSERT INTO system_state VALUES (?, ?)",
        (f"shorts_spam_blocked_until_7", str(time.time() - 60)),
    )
    db._conn.commit()
    assert _channel_shorts_spam_blocked(7, db) is False


# ── _count_clip_coverage ──────────────────────────────────────────

def test_coverage_counts_ready_short(db):
    # Long 101 has a pre-rendered 'ready' short → covered
    _insert_short(db._conn, 1, 3, 101, "ready")
    db._conn.commit()
    assert _count_clip_coverage(db, 3, [101, 102], 1) == 1


def test_coverage_counts_published_short(db):
    _insert_short(db._conn, 1, 3, 101, "published")
    db._conn.commit()
    assert _count_clip_coverage(db, 3, [101], 1) == 1


def test_coverage_counts_pending_slot_any_date(db):
    # A pending slot for a FUTURE date still covers the long's clip quota
    _insert_slot(db._conn, 1, 3, 101, "pending", short_id=5)
    db._conn.commit()
    assert _count_clip_coverage(db, 3, [101], 1) == 1


def test_coverage_avoids_double_count_short_plus_own_slot(db):
    # ready short + its own linked pending slot = ONE covered clip (MAX not SUM)
    _insert_short(db._conn, 1, 3, 101, "ready")
    _insert_slot(db._conn, 1, 3, 101, "pending", short_id=1)
    db._conn.commit()
    assert _count_clip_coverage(db, 3, [101], 1) == 1


def test_coverage_no_artifacts(db):
    assert _count_clip_coverage(db, 3, [101], 1) == 0


def test_coverage_no_longs(db):
    assert _count_clip_coverage(db, 3, [], 1) == 0


def test_coverage_caps_per_long(db):
    # clips_per_long=1 → even with 2 published shorts the long counts as 1
    _insert_short(db._conn, 1, 3, 101, "published")
    _insert_short(db._conn, 2, 3, 101, "published")
    db._conn.commit()
    assert _count_clip_coverage(db, 3, [101], 1) == 1
    # clips_per_long=3 → the same long counts as 2 (two published clips)
    assert _count_clip_coverage(db, 3, [101], 3) == 2


# ── _migrate_v42 (doomed unlinked clip slots cleanup) ─────────────

def _mig_slot(conn, slot_id, source_video_id, status="pending", short_id=None):
    conn.execute(
        """INSERT INTO shorts_planned_slots
           (id, channel_id, date_key, scheduled_at, target_upload_at,
            short_type, long_slot_position, source_video_id, short_id, status)
           VALUES (?, 3, '2026-08-20', '2026-08-20 17:13:00', '2026-08-20 17:43:00',
                   'clip', 1, ?, ?, ?)""",
        (slot_id, source_video_id, short_id, status),
    )


def test_migrate_v42_cancels_doomed(mig_conn):
    # slot 1: no source → doomed
    _mig_slot(mig_conn, 1, None)
    # slot 2: source 101 covered by a ready short → duplicate → doomed
    _mig_slot(mig_conn, 2, 101)
    mig_conn.execute(
        "INSERT INTO shorts (id, channel_id, source_video_id, type, status, title) "
        "VALUES (10, 3, 101, 'clip', 'ready', 't')"
    )
    # slot 3: source 102 uncovered + no local file → doomed
    _mig_slot(mig_conn, 3, 102)
    mig_conn.execute("INSERT INTO videos (id, video_path, status) VALUES (102, '', 'uploaded_private')")
    # slot 4: source 103 uncovered + local file exists → KEEP
    _mig_slot(mig_conn, 4, 103)
    mig_conn.execute(
        "INSERT INTO videos (id, video_path, status) VALUES (103, ?, 'awaiting_upload')",
        (__file__,),  # an existing file on disk
    )
    mig_conn.commit()

    import logging
    _migrate_v42(mig_conn, logging.getLogger("test"))

    statuses = {
        r["id"]: r["status"] for r in mig_conn.execute(
            "SELECT id, status FROM shorts_planned_slots"
        ).fetchall()
    }
    assert statuses == {1: "cancelled", 2: "cancelled", 3: "cancelled", 4: "pending"}


def test_migrate_v42_idempotent(mig_conn):
    _mig_slot(mig_conn, 1, None)
    mig_conn.commit()
    import logging
    _migrate_v42(mig_conn, logging.getLogger("test"))
    _migrate_v42(mig_conn, logging.getLogger("test"))  # second run: no-op
    row = mig_conn.execute("SELECT status FROM shorts_planned_slots WHERE id = 1").fetchone()
    assert row["status"] == "cancelled"
