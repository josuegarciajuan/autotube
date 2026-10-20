"""Contrato de publicación atómica de shorts planificados.

Cada slot es UN trabajo: se empieza a renderizar ``lead`` minutos antes de la
hora pública objetivo y se sube inmediatamente al terminar. No existe una
segunda cola de subida para los shorts nuevos.
"""

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from api.services import shorts_scheduler as ss


# ── 1. Hora de inicio derivada de la publicación ─────────────────


def test_start_is_publish_minus_lead_same_day():
    slot = ss._minutes_to_utc_slot(
        "2026-10-18", 10 * 60, 1, "canal2", "native",
        ZoneInfo("Europe/Madrid"), gen_lead_min=60,
    )
    # 10:00 Madrid (CEST) = 08:00 UTC; start 09:00 local = 07:00 UTC.
    assert slot["target_publish_at"] == "2026-10-18 08:00:00"
    assert slot["target_upload_at"] == "2026-10-18 08:00:00"
    assert slot["scheduled_at"] == "2026-10-18 07:00:00"
    assert slot["schedule_lead_min"] == 60
    assert slot["date_key"] == "2026-10-18"


def test_early_publish_starts_on_previous_day():
    """00:30 con lead 60 → el render empieza el día anterior a las 23:30."""
    slot = ss._minutes_to_utc_slot(
        "2026-10-18", 30, 1, "canal2", "native",
        ZoneInfo("Europe/Madrid"), gen_lead_min=60,
    )
    assert slot["target_publish_at"] == "2026-10-17 22:30:00"
    assert slot["scheduled_at"] == "2026-10-17 21:30:00"  # 23:30 Madrid
    assert slot["date_key"] == "2026-10-18", "date_key sigue siendo el día de publicación"


def test_zero_lead_never_moves_start_before_publish():
    slot = ss._minutes_to_utc_slot(
        "2026-10-18", 10 * 60, 1, "canal2", "native",
        ZoneInfo("Europe/Madrid"), gen_lead_min=0,
    )
    assert slot["scheduled_at"] == slot["target_publish_at"]


# ── 2. Lead time real por canal ──────────────────────────────────


def _timing_db(tmp_path):
    from database.db import init_db
    from database.db_extended import migrate_v2, _migrate_v69
    path = tmp_path / "lead.db"
    init_db(str(path))
    migrate_v2(str(path))
    import logging
    with sqlite3.connect(str(path)) as conn:
        _migrate_v69(conn, logging.getLogger("test"))
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase(str(path))
    with db._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channels (id, name, slug, config_json, active) "
            "VALUES (1, 'canal2', 'canal2', '{}', 1)"
        )
        conn.commit()
    return db


def _insert_job(db, channel_id, minutes):
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO generation_jobs
               (channel_id, action, status, started_at, finished_at,
                render_finished_at, upload_finished_at)
               VALUES (?, 'generate_native_short', 'completed',
                       '2026-10-01 10:00:00', datetime('2026-10-01 10:00:00', ?),
                       datetime('2026-10-01 10:00:00', ?), datetime('2026-10-01 10:00:00', ?))""",
            (channel_id, f"+{minutes} minutes", f"+{minutes - 5} minutes",
             f"+{minutes} minutes"),
        )
        conn.commit()


def test_lead_time_uses_channel_p90(tmp_path):
    from api.services import shorts_lead_time as lt

    db = _timing_db(tmp_path)
    for mins in range(20, 40):  # 20 samples, 20..39 min
        _insert_job(db, 1, mins)

    lt.invalidate_cache()
    lead = lt.estimate_channel_lead_minutes(1, db)
    # p90 of 20..39 = 37 min; + margin (10) = 47.
    assert lead == 47


def test_lead_time_falls_back_without_samples(tmp_path):
    from api.services import shorts_lead_time as lt

    db = _timing_db(tmp_path)
    lt.invalidate_cache()
    lead = lt.estimate_channel_lead_minutes(99, db)
    assert lead == lt.FALLBACK_LEAD_MIN


def test_lead_time_excludes_invalid_durations(tmp_path):
    from api.services import shorts_lead_time as lt

    db = _timing_db(tmp_path)
    for mins in range(20, 40):
        _insert_job(db, 1, mins)
    # Outliers: 5 s and 6 h must be ignored.
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO generation_jobs
               (channel_id, action, status, started_at, finished_at)
               VALUES (1, 'generate_native_short', 'completed',
                       '2026-10-01 10:00:00', '2026-10-01 10:00:05')"""
        )
        conn.execute(
            """INSERT INTO generation_jobs
               (channel_id, action, status, started_at, finished_at)
               VALUES (1, 'generate_native_short', 'completed',
                       '2026-10-01 10:00:00', '2026-10-01 16:00:00')"""
        )
        conn.commit()
    lt.invalidate_cache()
    assert lt.estimate_channel_lead_minutes(1, db) == 47


# ── 3. El planificador usa el lead real ──────────────────────────


def test_builder_derives_scheduled_at_from_channel_lead(monkeypatch):
    import database.db_extended as db_ext

    class _FakeDB:
        def get_optimal_slots(self, channel_id, content_type):
            return [{"slot_rank": 1, "target_hour": 10, "target_minute": 0}]

        def record_slot_usage(self, *a, **k):
            return True

    monkeypatch.setattr(db_ext, "ExtendedDatabase", lambda *a, **k: _FakeDB())
    monkeypatch.setattr(ss, "_channel_lead_minutes", lambda db, cid: 60)
    monkeypatch.setattr(
        "config.config_bridge.get_channel_config",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sin config")),
    )

    channel = {"id": 1, "slug": "one", "name": "One"}
    slots, _ = ss._build_shorts_slots_for_channel(
        channel, "2026-10-18", native_count=1,
        clips_per_long=0, long_video_count=0,
        long_target_hours=[], global_start_pos=0,
    )
    assert slots[0]["target_publish_at"] == "2026-10-18 08:00:00"
    assert slots[0]["scheduled_at"] == "2026-10-18 07:00:00"


# ── 4. Gate del trabajo atómico ──────────────────────────────────


def test_gate_blocks_when_quota_exhausted(monkeypatch):
    monkeypatch.setattr(ss, "_channel_shorts_spam_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ss, "_youtube_quota_blocked", lambda *a, **k: True)
    ok, reason = ss._planned_native_gate(1, "canal2", object())
    assert ok is False and "cuota" in reason


def test_gate_blocks_when_channel_target_already_met(monkeypatch):
    monkeypatch.setattr(ss, "_channel_shorts_spam_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ss, "_youtube_quota_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ss, "_channel_hard_daily_short_cap_reached", lambda *a, **k: False)
    monkeypatch.setattr(ss, "_global_shorts_daily_cap_reached", lambda *a, **k: False)

    class _DB:
        def count_native_shorts_published_today(self, cid):
            return 2

    monkeypatch.setattr(
        "api.services.channel_policy.resolve_channel_policy_values",
        lambda *a, **k: {"native_shorts_per_day": 2},
    )
    ok, reason = ss._planned_native_gate(1, "canal2", _DB())
    assert ok is False and "objetivo" in reason


# ── 5. Reparto global entre canales ──────────────────────────────


def test_global_spacing_moves_second_channel_to_next_franja(monkeypatch):
    class _DB:
        def get_optimal_slots(self, channel_id, content_type):
            return [
                {"slot_rank": 1, "target_hour": 10, "target_minute": 0},
                {"slot_rank": 2, "target_hour": 13, "target_minute": 0},
            ]

        def get_channel_planning_config(self, channel_id):
            return {}

    monkeypatch.setattr(ss, "_same_type_gap_minutes", lambda: 120)
    monkeypatch.setattr(ss, "_global_publish_spacing_minutes", lambda db: 45)
    monkeypatch.setattr(ss, "_channel_lead_minutes", lambda db, cid: 60)
    monkeypatch.setattr(
        "config.config_bridge.get_channel_config",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sin config")),
    )

    slots = [
        {"channel_id": 1, "channel_slug": "one", "slot_rank": 1,
         "target_publish_at": "2026-10-18 08:00:00",
         "target_upload_at": "2026-10-18 08:00:00",
         "scheduled_at": "2026-10-18 07:00:00", "schedule_lead_min": 60},
        {"channel_id": 2, "channel_slug": "two", "slot_rank": 1,
         "target_publish_at": "2026-10-18 08:00:00",
         "target_upload_at": "2026-10-18 08:00:00",
         "scheduled_at": "2026-10-18 07:00:00", "schedule_lead_min": 60},
    ]
    out = ss._spread_shorts_globally(slots, _DB())
    moved = [s for s in out if s.get("globally_retimed")]
    assert len(moved) == 1
    # 10:00 → 13:00 Madrid = 11:00 UTC; start 10:00 UTC.
    assert moved[0]["target_publish_at"] == "2026-10-18 11:00:00"
    assert moved[0]["scheduled_at"] == "2026-10-18 10:00:00"


# ── 6. Backlog heredado intacto ─────────────────────────────────


def test_reconcile_does_not_touch_generated_backlog(tmp_path, monkeypatch):
    db = _timing_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO shorts (channel_id, type, title, status, file_path)
               VALUES (1, 'native', 'legacy', 'generated', '/tmp/legacy.mp4')"""
        )
        conn.commit()

    monkeypatch.setattr(ss, "compute_daily_shorts_slots", lambda day, db: [])
    ss.reconcile_shorts_schedule(db=db, horizon_days=1, reason="test")

    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, youtube_id FROM shorts WHERE title='legacy'"
        ).fetchone()
    assert row["status"] == "generated"
    assert row["youtube_id"] is None


# ── 7. Migración v69 ────────────────────────────────────────────


def test_migration_v69_adds_and_backfills_columns(tmp_path):
    from database.db_extended import _migrate_v69
    import logging

    path = tmp_path / "mig69.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE shorts_planned_slots (
                id INTEGER PRIMARY KEY,
                scheduled_at TEXT,
                target_upload_at TEXT
            );
            CREATE TABLE generation_jobs (
                id INTEGER PRIMARY KEY,
                started_at TEXT,
                finished_at TEXT
            );
            INSERT INTO shorts_planned_slots (id, scheduled_at, target_upload_at)
            VALUES (1, '2026-10-18 07:00:00', '2026-10-18 08:00:00');
        """)
        _migrate_v69(conn, logging.getLogger("test"))
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('shorts_planned_slots')")}
        assert "target_publish_at" in cols
        assert conn.execute(
            "SELECT target_publish_at FROM shorts_planned_slots WHERE id=1"
        ).fetchone()[0] == "2026-10-18 08:00:00"

        job_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('generation_jobs')")}
        assert {"render_finished_at", "upload_started_at", "upload_finished_at"} <= job_cols

        # Idempotent
        _migrate_v69(conn, logging.getLogger("test"))


# ── 8. Transición: slots antiguos alinean su hora de inicio ─────


def test_normalize_aligns_existing_pending_start(tmp_path, monkeypatch):
    db = _timing_db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO shorts_planned_slots
               (channel_id, date_key, scheduled_at, target_upload_at,
                target_publish_at, short_type, status)
               VALUES (1, '2026-10-21', '2026-10-21 07:45:00',
                       '2026-10-21 08:00:00', '2026-10-21 08:00:00',
                       'native', 'pending')"""
        )
        conn.commit()
    monkeypatch.setattr(ss, "_channel_lead_minutes", lambda db, cid: 60)

    assert ss.normalize_pending_shorts_starts(db) == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT scheduled_at, target_publish_at FROM shorts_planned_slots "
            "WHERE date_key='2026-10-21'"
        ).fetchone()
    assert row["scheduled_at"] == "2026-10-21 07:00:00", "publicación intacta, inicio −60m"
    assert row["target_publish_at"] == "2026-10-21 08:00:00"
