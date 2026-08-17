"""Per-account (per-project) quota alert + breaker independence tests.

Fase cuota (ago 2026): hay 2 cuentas Google → 2 proyectos GCP
  - tracatrack        → youtube-uploads-automation  → canal2, canal3
  - burrianacasa2026  → autotube-expediciones       → canal4, canal5
Cada cuenta debe tener SU propia alerta quota_exhausted y su propio breaker.
"""

import sqlite3

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services.lifecycle_monitor import create_alert
from api.services.quota_tracker import project_entity_id

_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
)
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


def _db(tmp_path):
    path = tmp_path / "quota_alerts.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.executescript(_PIPELINE_ALERTS_DDL)
    return ExtendedDatabase(str(path))


def test_project_entity_id_is_stable_and_distinct():
    a = project_entity_id("youtube-uploads-automation")
    b = project_entity_id("autotube-expediciones")
    assert a != b
    assert a > 0 and b > 0
    # Stable across calls
    assert project_entity_id("youtube-uploads-automation") == a
    # Unknown/legacy groups map to 0 (old single-alert behavior)
    assert project_entity_id("unknown") == 0
    assert project_entity_id("") == 0


def test_quota_alerts_are_independent_per_project(tmp_path):
    db = _db(tmp_path)
    eid_a = project_entity_id("youtube-uploads-automation")
    eid_b = project_entity_id("autotube-expediciones")

    id_a = create_alert(
        db, entity_type="system", entity_id=eid_a, channel_id=None,
        alert_type="quota_exhausted", severity="critical",
        title="YouTube API quota agotada — youtube-uploads-automation",
        message="Cuota agotada para tracatrack",
        metadata={"project_id": "youtube-uploads-automation"},
    )
    id_b = create_alert(
        db, entity_type="system", entity_id=eid_b, channel_id=None,
        alert_type="quota_exhausted", severity="critical",
        title="YouTube API quota agotada — autotube-expediciones",
        message="Cuota agotada para burrianacasa2026",
        metadata={"project_id": "autotube-expediciones"},
    )

    # Both projects got their own alert (the bug: they used to collapse into one)
    assert id_a is not None and id_b is not None
    assert id_a != id_b

    # Same project trips again → dedup (no new alert, message updated)
    again = create_alert(
        db, entity_type="system", entity_id=eid_a, channel_id=None,
        alert_type="quota_exhausted", severity="critical",
        title="same", message="updated",
    )
    assert again is None

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT entity_id, title FROM pipeline_alerts "
            "WHERE alert_type='quota_exhausted' AND resolved=0"
        ).fetchall()
    assert {r["entity_id"] for r in rows} == {eid_a, eid_b}


def test_clear_quota_exhausted_only_clears_target_project(tmp_path):
    db = _db(tmp_path)
    db.set_system_state(
        "quota_exhausted_youtube-uploads-automation", "2026-08-17T10:00:00+00:00"
    )
    db.set_system_state(
        "quota_exhausted_autotube-expediciones", "2026-08-17T11:00:00+00:00"
    )
    db.set_system_state("quota_exhausted_at", "2026-08-17T11:00:00+00:00")

    db.clear_quota_exhausted(project_id="youtube-uploads-automation")

    assert not db.is_project_quota_exhausted("youtube-uploads-automation")
    assert db.is_project_quota_exhausted("autotube-expediciones")
    # Global summary now reflects the remaining exhausted project
    assert db.get_system_state("quota_exhausted_at") == "2026-08-17T11:00:00+00:00"
    assert db.is_quota_exhausted() is True
    assert db.get_exhausted_projects() == ["autotube-expediciones"]

    # Recovering the last project clears the global summary
    db.clear_quota_exhausted(project_id="autotube-expediciones")
    assert not db.is_quota_exhausted()
    assert db.get_exhausted_projects() == []
    assert db.get_system_state("quota_exhausted_at") == ""


def test_get_quota_reset_time_per_project(tmp_path):
    db = _db(tmp_path)
    db.set_system_state(
        "quota_exhausted_youtube-uploads-automation", "2026-08-17T12:00:00+00:00"
    )

    info = db.get_quota_reset_time(project_id="youtube-uploads-automation")
    assert info["exhausted"] is True
    assert info["reset_at_utc"] > "2026-08-17T12:00:00+00:00"

    # A project without a breaker key → not exhausted
    info2 = db.get_quota_reset_time(project_id="autotube-expediciones")
    assert info2["exhausted"] is False
