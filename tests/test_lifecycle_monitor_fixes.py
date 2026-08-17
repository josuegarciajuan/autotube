"""Regression tests for lifecycle_monitor fixes.

Covers two bugs observed in production logs (logs/api.log):

1. ``_maybe_create_alert() missing 1 required positional argument: 'channel_id'``
   — the 3 calls inside ``_check_llm_credits`` omitted the required
   ``channel_id`` arg, so the TypeError was swallowed by the except and
   LLM credit alerts were NEVER created (silent loss).

2. ``CHECK constraint failed: entity_type IN ('video', 'short')``
   — system audit events (``quota_recovered``) are logged via ``log_event``
   with ``entity_type='system'``, but the original ``lifecycle_events`` CHECK
   only allowed ``('video', 'short')``. Migration v41 widens it to
   ``('video', 'short', 'system')`` (same pattern as v40 for pipeline_alerts).
"""

import logging
import sqlite3

from database.db import init_db
from database.db_extended import ExtendedDatabase, _migrate_v41
from api.services.lifecycle_monitor import _check_llm_credits, log_event

_LIFECYCLE_EVENTS_DDL = """
-- Minimal channels table so the FK reference in lifecycle_events resolves
-- (db._connect() enables PRAGMA foreign_keys=ON; the real channels table
-- comes from schema_v2.sql, which init_db does not run).
CREATE TABLE IF NOT EXISTS channels (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug    TEXT,
    name    TEXT
);
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL CHECK(entity_type IN ('video', 'short')),
    entity_id       INTEGER NOT NULL,
    channel_id      INTEGER,
    event           TEXT NOT NULL,
    phase           TEXT,
    status          TEXT NOT NULL DEFAULT 'started',
    message         TEXT,
    metadata_json   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_entity ON lifecycle_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_channel ON lifecycle_events(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_event ON lifecycle_events(event, created_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_time ON lifecycle_events(created_at);
"""

_PIPELINE_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL CHECK(entity_type IN ('video', 'short', 'system')),
    entity_id       INTEGER,
    channel_id      INTEGER,
    alert_type      TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'warning',
    title           TEXT NOT NULL,
    message         TEXT,
    metadata_json   TEXT,
    acknowledged    BOOLEAN DEFAULT 0,
    resolved        BOOLEAN DEFAULT 0,
    resolved_at     TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_unique_active
ON pipeline_alerts(entity_type, entity_id, alert_type)
WHERE resolved = 0;
"""


def _build_db(tmp_path, ddl: str) -> ExtendedDatabase:
    """Create a fresh DB file with base schema + the given monitoring DDL."""
    path = tmp_path / "lifecycle_fixes_test.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(ddl)
    return ExtendedDatabase(str(path))


# ═══════════════════════════════════════════════════════════════
# Bug 1: LLM credit alerts must pass channel_id to _maybe_create_alert
# ═══════════════════════════════════════════════════════════════

def test_llm_credits_alert_creates_with_channel_id(monkeypatch, tmp_path):
    db = _build_db(tmp_path, _PIPELINE_ALERTS_DDL)

    # Fake the LLM credit check → DeepSeek exhausted (would create an alert).
    import api.services.llm_credit_checker as lcc
    fake_status = {
        "deepseek": {"status": "exhausted", "balance_usd": 0.0, "currency": "USD"},
        "openai": None,
        "youtube": None,
    }
    monkeypatch.setattr(lcc, "check_all_llm_credits", lambda db, force=False: fake_status)

    created = _check_llm_credits(db)

    # Before the fix, _maybe_create_alert raised TypeError (missing channel_id),
    # was swallowed, and created == 0. After the fix an alert row must exist.
    assert created == 1

    with db._connect() as conn:
        row = conn.execute(
            "SELECT entity_type, entity_id, channel_id, alert_type, severity, title "
            "FROM pipeline_alerts WHERE alert_type = 'llm_credit_exhausted'"
        ).fetchone()
    assert row is not None
    assert row["entity_type"] == "system"
    assert row["entity_id"] == 0
    assert row["channel_id"] is None  # system-wide alert, not bound to a channel
    assert "DeepSeek" in row["title"]


def test_llm_credits_dedup_respects_channel_id(monkeypatch, tmp_path):
    """Running the check twice must not duplicate the alert (dedup still works)."""
    db = _build_db(tmp_path, _PIPELINE_ALERTS_DDL)

    import api.services.llm_credit_checker as lcc
    fake_status = {
        "deepseek": {"status": "exhausted", "balance_usd": 0.0, "currency": "USD"},
        "openai": None,
        "youtube": None,
    }
    monkeypatch.setattr(lcc, "check_all_llm_credits", lambda db, force=False: fake_status)

    assert _check_llm_credits(db) == 1
    assert _check_llm_credits(db) == 0  # dedup → no new alert

    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) as c FROM pipeline_alerts WHERE alert_type = 'llm_credit_exhausted'"
        ).fetchone()["c"]
    assert n == 1


# ═══════════════════════════════════════════════════════════════
# Bug 2: lifecycle_events CHECK must allow entity_type='system' (v41)
# ═══════════════════════════════════════════════════════════════

def test_lifecycle_events_check_allows_system_after_v41(tmp_path):
    db = _build_db(tmp_path, _LIFECYCLE_EVENTS_DDL)

    # Sanity: with the ORIGINAL CHECK, system events are rejected (the bug).
    log_event(db, entity_type='system', entity_id=0, channel_id=None,
              event='quota_recovered', status='info', message='should fail pre-v41')
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) as c FROM lifecycle_events WHERE entity_type = 'system'"
        ).fetchone()["c"]
    assert n == 0  # insert was rejected by the CHECK constraint

    # Apply migration v41 (widens CHECK to ('video', 'short', 'system')).
    with sqlite3.connect(str(db.db_path)) as conn:
        _migrate_v41(conn, logging.getLogger("test"))

    with db._connect() as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lifecycle_events'"
        ).fetchone()[0]
    assert "entity_type IN ('video', 'short', 'system')" in ddl

    # System events now insert cleanly (no CHECK violation).
    log_event(db, entity_type='system', entity_id=0, channel_id=None,
              event='quota_recovered', status='info', message='post-v41 ok')
    with db._connect() as conn:
        row = conn.execute(
            "SELECT entity_type, entity_id, event FROM lifecycle_events "
            "WHERE entity_type = 'system'"
        ).fetchone()
    assert row is not None
    assert row["event"] == "quota_recovered"

    # Idempotent: running v41 again is a no-op.
    with sqlite3.connect(str(db.db_path)) as conn:
        _migrate_v41(conn, logging.getLogger("test"))
    with db._connect() as conn:
        ddl2 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lifecycle_events'"
        ).fetchone()[0]
    assert "entity_type IN ('video', 'short', 'system')" in ddl2


def test_video_events_survive_v41_rebuild(tmp_path):
    """v41 rebuild must preserve existing video/short rows and indexes."""
    db = _build_db(tmp_path, _LIFECYCLE_EVENTS_DDL)

    log_event(db, entity_type='video', entity_id=42, channel_id=7,
              event='upload_completed', phase='upload', status='completed',
              message='ok', metadata={'duration_ms': 1000})

    with sqlite3.connect(str(db.db_path)) as conn:
        _migrate_v41(conn, logging.getLogger("test"))

    with db._connect() as conn:
        row = conn.execute(
            "SELECT entity_type, entity_id, channel_id, event FROM lifecycle_events "
            "WHERE entity_id = 42"
        ).fetchone()
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='lifecycle_events'"
        ).fetchall()
    assert row is not None
    assert row["entity_type"] == "video"
    assert row["event"] == "upload_completed"
    assert {i["name"] for i in idx} >= {
        "idx_lifecycle_entity", "idx_lifecycle_channel",
        "idx_lifecycle_event", "idx_lifecycle_time",
    }
