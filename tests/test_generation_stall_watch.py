"""Regression tests for the heartbeat-independent generation stall detector.

`_check_video_phase_stuck` fía el "no atascado" al heartbeat del worker, que
sigue latiendo aunque un render/concat distribuido delegado esté encallado. La
nueva `_check_generation_progress_freeze` mira la firma phase|pipeline|progress
contra un snapshot persistido en system_state y alerta si no cambia.
"""

import sqlite3
from datetime import datetime, timedelta

from database.db import init_db
from database.db_extended import ExtendedDatabase

_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS generation_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER,
    video_id        INTEGER,
    action          TEXT,
    status          TEXT,
    phase           TEXT,
    pipeline_phase  TEXT,
    progress        INTEGER
);
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _build_db(tmp_path) -> ExtendedDatabase:
    path = tmp_path / "gen_stall_test.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_DDL)
        conn.execute(
            "INSERT INTO generation_jobs "
            "(id, channel_id, video_id, action, status, phase, pipeline_phase, progress) "
            "VALUES (10, 1, 100, 'generate_only', 'running', 'video', 'render', 57)"
        )
        conn.commit()
    return ExtendedDatabase(str(path))


def test_frozen_progress_raises_alert_after_threshold(monkeypatch, tmp_path):
    from api.services import lifecycle_monitor as lm

    db = _build_db(tmp_path)
    clock = {"now": datetime(2026, 10, 6, 12, 0, 0)}
    monkeypatch.setattr(lm, "_utcnow", lambda: clock["now"])

    # First scan only seeds the snapshot → no alert yet.
    assert lm._check_generation_progress_freeze(db) == 0

    # 26 min later, still frozen → alert.
    clock["now"] = clock["now"] + timedelta(minutes=26)
    assert lm._check_generation_progress_freeze(db) == 1

    with db._connect() as conn:
        row = conn.execute(
            "SELECT entity_type, entity_id, alert_type, severity, resolved "
            "FROM pipeline_alerts WHERE alert_type = 'generation_stalled'"
        ).fetchone()
    assert row is not None
    assert row["entity_type"] == "video"
    assert row["entity_id"] == 100
    assert row["severity"] == "critical"
    assert row["resolved"] == 0


def test_progress_resets_clock_and_resolves_alert(monkeypatch, tmp_path):
    from api.services import lifecycle_monitor as lm

    db = _build_db(tmp_path)
    clock = {"now": datetime(2026, 10, 6, 12, 0, 0)}
    monkeypatch.setattr(lm, "_utcnow", lambda: clock["now"])

    assert lm._check_generation_progress_freeze(db) == 0
    clock["now"] = clock["now"] + timedelta(minutes=26)
    assert lm._check_generation_progress_freeze(db) == 1

    # The job advances → next scan must resolve the stall alert.
    with db._connect() as conn:
        conn.execute("UPDATE generation_jobs SET progress = 60 WHERE id = 10")
        conn.commit()
    clock["now"] = clock["now"] + timedelta(minutes=1)
    assert lm._check_generation_progress_freeze(db) == 0

    with db._connect() as conn:
        row = conn.execute(
            "SELECT resolved FROM pipeline_alerts WHERE alert_type = 'generation_stalled'"
        ).fetchone()
    assert row["resolved"] == 1


def test_no_alert_below_threshold(monkeypatch, tmp_path):
    from api.services import lifecycle_monitor as lm

    db = _build_db(tmp_path)
    clock = {"now": datetime(2026, 10, 6, 12, 0, 0)}
    monkeypatch.setattr(lm, "_utcnow", lambda: clock["now"])

    assert lm._check_generation_progress_freeze(db) == 0
    clock["now"] = clock["now"] + timedelta(minutes=20)  # < 25
    assert lm._check_generation_progress_freeze(db) == 0


def test_kill_switch_disables_check(monkeypatch, tmp_path):
    from api.services import lifecycle_monitor as lm

    db = _build_db(tmp_path)
    monkeypatch.setattr(lm, "GENERATION_STALL_WATCH", False)
    assert lm._check_generation_progress_freeze(db) == 0
