"""Contract tests for the non-destructive full-replan flow."""

import json
import sqlite3
from datetime import date

import pytest

from api.services.planning_service import safe_full_replan_apply, safe_full_replan_preflight
from database.db_extended import ExtendedDatabase, _migrate_v39


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "safe-replan.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT NOT NULL,
            config_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE generation_jobs (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            video_id INTEGER,
            status TEXT NOT NULL DEFAULT 'queued'
        );
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            status TEXT NOT NULL DEFAULT 'draft'
        );
        CREATE TABLE planned_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            date_key TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            target_upload_at TEXT,
            target_public_at TEXT,
            upload_window_start INTEGER,
            upload_window_end INTEGER,
            slot_position INTEGER DEFAULT 0,
            source_mode TEXT DEFAULT 'original',
            status TEXT NOT NULL DEFAULT 'pending',
            job_id INTEGER
        );
        CREATE TABLE shorts_planned_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            date_key TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            target_upload_at TEXT,
            short_type TEXT NOT NULL DEFAULT 'native',
            long_slot_position INTEGER,
            source_video_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            job_id INTEGER,
            short_id INTEGER,
            slot_position INTEGER DEFAULT 0
        );
        CREATE TABLE shorts_planning_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL UNIQUE,
            shorts_native_per_day INTEGER DEFAULT 3,
            shorts_clip_per_day INTEGER DEFAULT 0,
            shorts_clips_per_long INTEGER DEFAULT 0,
            shorts_enabled INTEGER DEFAULT 1
        );
        """
    )
    conn.execute(
        "INSERT INTO channels (id, name, slug, config_json, active) VALUES (1, 'Channel', 'channel', ?, 1)",
        (json.dumps({"videos_per_day": 1, "planning_enabled": True}),),
    )
    conn.execute("INSERT INTO shorts_planning_config (channel_id) VALUES (1)")
    conn.execute("INSERT INTO generation_jobs (id, channel_id, status) VALUES (41, 1, 'queued')")
    conn.execute(
        """INSERT INTO planned_slots
           (channel_id, date_key, scheduled_at, target_upload_at, status, job_id)
           VALUES (1, ?, '2030-01-01 08:00:00', '2030-01-01 10:00:00', 'pending', 41)""",
        (date.today().isoformat(),),
    )
    _migrate_v39(conn, __import__("logging").getLogger(__name__))
    conn.commit()
    conn.close()

    database = ExtendedDatabase()
    database._db_path = str(path)

    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    database._connect = connect
    return database


def _rows(db):
    with db._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM planned_slots ORDER BY id")]


def _short_rows(db):
    with db._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM shorts_planned_slots ORDER BY id")]


def test_preflight_is_read_only_and_persists_an_opaque_confirmation(db):
    before = _rows(db)

    result = safe_full_replan_preflight(db=db, horizon_days=1)

    assert result["confirmation_token"]
    assert result["proposed_slots"]
    assert _rows(db) == before
    with db._connect() as conn:
        stored = conn.execute("SELECT token_hash FROM safe_replan_confirmations").fetchone()
    assert stored is not None
    assert stored["token_hash"] != result["confirmation_token"]


def test_apply_updates_pending_slots_in_place_without_cancelling_jobs(db):
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    original = _rows(db)[0]

    result = safe_full_replan_apply(preflight["confirmation_token"], db=db)

    updated = _rows(db)[0]
    assert result["ok"] is True
    assert result["updated"] == 1
    assert updated["id"] == original["id"]
    assert updated["job_id"] == 41
    with db._connect() as conn:
        assert conn.execute("SELECT status FROM generation_jobs WHERE id = 41").fetchone()["status"] == "queued"


def test_apply_rejects_stale_and_reused_confirmation_tokens(db):
    stale = safe_full_replan_preflight(db=db, horizon_days=1)
    with db._connect() as conn:
        conn.execute("UPDATE planned_slots SET scheduled_at = '2030-01-02 08:00:00' WHERE id = 1")
        conn.commit()

    with pytest.raises(ValueError, match="stale"):
        safe_full_replan_apply(stale["confirmation_token"], db=db)

    fresh = safe_full_replan_preflight(db=db, horizon_days=1)
    safe_full_replan_apply(fresh["confirmation_token"], db=db)
    with pytest.raises(ValueError, match="used"):
        safe_full_replan_apply(fresh["confirmation_token"], db=db)


def test_preflight_reviews_every_pending_backlog_slot_and_reports_actual_counts(db):
    """Pending slots outside the generated horizon are retained explicitly."""
    with db._connect() as conn:
        for day in range(2, 10):
            conn.execute("""
                INSERT INTO planned_slots
                    (channel_id, date_key, scheduled_at, target_upload_at, status)
                VALUES (1, ?, ?, ?, 'pending')
            """, (
                f"2040-01-{day:02d}",
                f"2040-01-{day:02d} 08:00:00",
                f"2040-01-{day:02d} 10:00:00",
            ))
        conn.commit()

    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    review = preflight["review"]
    reviewed_ids = {item["slot_id"] for item in review if item["slot_id"] is not None}

    assert reviewed_ids == set(range(1, 10))
    assert preflight["counts"]["retained"] + preflight["counts"]["rescheduled"] == 9
    assert preflight["counts"]["retained"] > 0
    assert preflight["counts"]["new"] == 0

    applied = safe_full_replan_apply(preflight["confirmation_token"], db=db)

    assert applied["counts"] == preflight["counts"]
    assert {row["id"] for row in _rows(db)} == set(range(1, 10))
    assert applied["review"] == review


def test_active_job_or_video_change_makes_confirmation_stale(db):
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    with db._connect() as conn:
        conn.execute("INSERT INTO videos (id, channel_id, status) VALUES (9, 1, 'generating')")
        conn.execute("INSERT INTO generation_jobs (id, channel_id, video_id, status) VALUES (42, 1, 9, 'running')")
        conn.commit()

    with pytest.raises(ValueError, match="stale"):
        safe_full_replan_apply(preflight["confirmation_token"], db=db)


def test_safe_replan_retimes_pending_shorts_before_creating_new_shorts(db):
    """Pending Shorts keep identity; running Shorts remain wholly untouched."""
    today = date.today().isoformat()
    with db._connect() as conn:
        conn.execute("""
            INSERT INTO shorts_planned_slots
                (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                 long_slot_position, source_video_id, status, job_id, short_id, slot_position)
            VALUES (1, ?, '2030-01-01 08:00:00', '2030-01-01 08:15:00', 'clip',
                    7, 22, 'pending', 51, 61, 3)
        """, (today,))
        conn.execute("""
            INSERT INTO shorts_planned_slots
                (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                 status, job_id, short_id, slot_position)
            VALUES (1, ?, '2030-01-01 09:00:00', '2030-01-01 09:15:00', 'native',
                    'running', 52, 62, 4)
        """, (today,))
        conn.commit()

    original_pending, original_running = _short_rows(db)
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)

    assert preflight["covers"] == "long-form + Shorts"
    assert preflight["counts"]["shorts"]["retained"] + preflight["counts"]["shorts"]["rescheduled"] == 1
    assert preflight["summary"]["shorts"]["proposed"] >= 1

    applied = safe_full_replan_apply(preflight["confirmation_token"], db=db)

    pending, running = _short_rows(db)
    assert pending["id"] == original_pending["id"]
    assert pending["job_id"] == original_pending["job_id"]
    assert pending["short_id"] == original_pending["short_id"]
    assert pending["source_video_id"] == original_pending["source_video_id"]
    assert pending["long_slot_position"] == original_pending["long_slot_position"]
    assert running == original_running
    assert applied["counts"]["shorts"] == preflight["counts"]["shorts"]


def test_pending_or_running_shorts_change_makes_confirmation_stale(db):
    today = date.today().isoformat()
    with db._connect() as conn:
        conn.execute("""
            INSERT INTO shorts_planned_slots
                (channel_id, date_key, scheduled_at, short_type, status)
            VALUES (1, ?, '2030-01-01 08:00:00', 'native', 'pending')
        """, (today,))
        conn.commit()

    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    with db._connect() as conn:
        conn.execute("UPDATE shorts_planned_slots SET status = 'running' WHERE id = 1")
        conn.commit()

    with pytest.raises(ValueError, match="stale"):
        safe_full_replan_apply(preflight["confirmation_token"], db=db)


def test_legacy_full_replan_endpoint_is_gone():
    from api.routers.planning import full_replan
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        full_replan()

    assert exc_info.value.status_code == 410
