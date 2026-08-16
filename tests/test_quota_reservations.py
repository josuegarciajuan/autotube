"""Offline tests for transactional YouTube quota reservations."""

import sqlite3

from database.db_extended import ExtendedDatabase


def _db(tmp_path):
    path = tmp_path / "quota.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE yt_quota_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            quota_day_pt TEXT NOT NULL,
            operation TEXT NOT NULL,
            content_class TEXT NOT NULL,
            units INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved',
            reference_id TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finalized_at TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return ExtendedDatabase(str(path))


def test_reservation_preserves_essential_margin(tmp_path):
    db = _db(tmp_path)

    first = db.reserve_youtube_quota(
        project_id="project-a", quota_day_pt="2026-08-15",
        operation="videos.insert", content_class="long", units=4800,
        reference_id="job-1", automatic_budget=8000,
    )
    second = db.reserve_youtube_quota(
        project_id="project-a", quota_day_pt="2026-08-15",
        operation="videos.insert", content_class="short", units=3200,
        reference_id="job-2", automatic_budget=8000,
    )
    rejected = db.reserve_youtube_quota(
        project_id="project-a", quota_day_pt="2026-08-15",
        operation="videos.insert", content_class="short", units=1600,
        reference_id="job-3", automatic_budget=8000,
    )

    assert first["granted"] is True
    assert second["granted"] is True
    assert rejected["granted"] is False
    assert rejected["reason"] == "automatic_budget_exhausted"


def test_reservation_is_idempotent_for_same_reference(tmp_path):
    db = _db(tmp_path)

    first = db.reserve_youtube_quota(
        project_id="project-a", quota_day_pt="2026-08-15",
        operation="videos.insert", content_class="long", units=1600,
        reference_id="job-1", automatic_budget=8000,
    )
    repeated = db.reserve_youtube_quota(
        project_id="project-a", quota_day_pt="2026-08-15",
        operation="videos.insert", content_class="long", units=1600,
        reference_id="job-1", automatic_budget=8000,
    )

    assert first["granted"] is True
    assert repeated["granted"] is True
    assert repeated["reservation_id"] == first["reservation_id"]


def test_consumed_reservation_cannot_admit_a_second_upload(tmp_path):
    db = _db(tmp_path)
    reserved = db.reserve_youtube_quota(
        project_id="project-a", quota_day_pt="2026-08-15",
        operation="videos.insert", content_class="long", units=1600,
        reference_id="video-10", automatic_budget=8000,
    )
    db.finalize_youtube_quota_reservation(reserved["reservation_id"], consumed=True)

    repeated = db.reserve_youtube_quota(
        project_id="project-a", quota_day_pt="2026-08-15",
        operation="videos.insert", content_class="long", units=1600,
        reference_id="video-10", automatic_budget=8000,
    )

    assert repeated == {"granted": False, "reason": "already_consumed"}


def test_remediation_mode_defaults_to_safe():
    """El DEFAULT en código de YT_REMEDIATION_MODE debe ser safe-on ("true").

    El .env de producción puede desactivarlo explícitamente tras validar el
    preflight (load_dotenv override=True), así que verificamos el default del
    código fuente, no el valor efectivo del entorno.
    """
    import re
    from pathlib import Path

    src = Path("config/settings.py").read_text()

    m = re.search(
        r'YT_REMEDIATION_MODE = os\.getenv\("YT_REMEDIATION_MODE", "([^"]+)"\)',
        src,
    )
    assert m, "YT_REMEDIATION_MODE definition not found"
    assert m.group(1).lower() == "true"

    m2 = re.search(
        r'SHORTS_CHAIN_DISPATCH_ENABLED = os\.getenv\("SHORTS_CHAIN_DISPATCH_ENABLED", "([^"]+)"\)',
        src,
    )
    assert m2, "SHORTS_CHAIN_DISPATCH_ENABLED definition not found"
    assert m2.group(1).lower() == "false"


def test_preflight_prioritizes_oldest_long_backlog(tmp_path):
    from api.services.quota_preflight import build_project_preflight

    path = tmp_path / "preflight.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, active INTEGER);
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY, channel_id INTEGER, status TEXT,
            created_at TEXT, target_public_at TEXT, yt_video_id TEXT
        );
    """)
    conn.execute("INSERT INTO channels VALUES (1, 'canal2', 1)")
    conn.execute("INSERT INTO channels VALUES (2, 'canal3', 1)")
    conn.execute("INSERT INTO videos VALUES (10, 1, 'awaiting_upload', '2026-08-10 10:00:00', NULL, NULL)")
    conn.execute("INSERT INTO videos VALUES (11, 2, 'awaiting_upload', '2026-08-11 10:00:00', NULL, NULL)")
    conn.commit()
    conn.close()

    result = build_project_preflight(str(path), {"canal2": "project-a", "canal3": "project-a"})

    assert result["projects"]["project-a"]["eligible_long_video_ids"] == [10, 11]
    assert result["projects"]["project-a"]["automatic_upload_capacity"] == 5
