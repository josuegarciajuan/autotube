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
    monkeypatch.setattr(ss, "_short_schedule_lead_minutes", lambda db, cid: 60)
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


def test_global_spacing_prefers_another_franja_same_day(monkeypatch):
    """Entre canales se evita el solape si hay otra franja libre, sin cambiar de día."""
    import database.db_extended as db_ext

    class _FakeDB:
        def get_optimal_slots(self, channel_id, content_type):
            return [
                {"slot_rank": 1, "target_hour": 10, "target_minute": 0},
                {"slot_rank": 2, "target_hour": 13, "target_minute": 0},
                {"slot_rank": 3, "target_hour": 18, "target_minute": 0},
            ]

        def record_slot_usage(self, *a, **k):
            return True

    monkeypatch.setattr(db_ext, "ExtendedDatabase", lambda *a, **k: _FakeDB())
    monkeypatch.setattr(ss, "_short_schedule_lead_minutes", lambda db, cid: 60)
    monkeypatch.setattr(
        "config.config_bridge.get_channel_config",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sin config")),
    )
    channel = {"id": 2, "slug": "two", "name": "Two"}
    busy = [
        datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc),  # 10:00 Madrid
    ]
    slots, _ = ss._build_shorts_slots_for_channel(
        channel, "2026-10-18", native_count=2,
        clips_per_long=0, long_video_count=0,
        long_target_hours=[], global_start_pos=0,
        global_busy=busy,
    )
    # Evita las 10:00 y usa dos franjas distintas del mismo día.
    publish = [s_["target_publish_at"] for s_ in slots]
    assert all(p.startswith("2026-10-18") for p in publish)
    assert "2026-10-18 08:00:00" not in publish


def test_overlap_is_allowed_when_all_franjas_busy(monkeypatch):
    """Si no hay franja libre, se permite el solape (no se cambia de día)."""
    import database.db_extended as db_ext

    class _FakeDB:
        def get_optimal_slots(self, channel_id, content_type):
            return [
                {"slot_rank": 1, "target_hour": 10, "target_minute": 0},
                {"slot_rank": 2, "target_hour": 13, "target_minute": 0},
                {"slot_rank": 3, "target_hour": 18, "target_minute": 0},
            ]

        def record_slot_usage(self, *a, **k):
            return True

    monkeypatch.setattr(db_ext, "ExtendedDatabase", lambda *a, **k: _FakeDB())
    monkeypatch.setattr(ss, "_short_schedule_lead_minutes", lambda db, cid: 60)
    monkeypatch.setattr(
        "config.config_bridge.get_channel_config",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sin config")),
    )
    busy = [
        datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc),
    ]
    channel = {"id": 2, "slug": "two", "name": "Two"}
    slots, _ = ss._build_shorts_slots_for_channel(
        channel, "2026-10-18", native_count=3,
        clips_per_long=0, long_video_count=0,
        long_target_hours=[], global_start_pos=0,
        global_busy=busy,
    )
    assert len(slots) == 3
    assert all(s_["target_publish_at"].startswith("2026-10-18") for s_ in slots)


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
    monkeypatch.setattr(ss, "_short_schedule_lead_minutes", lambda db, cid: 60)

    assert ss.normalize_pending_shorts_starts(db) == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT scheduled_at, target_publish_at FROM shorts_planned_slots "
            "WHERE date_key='2026-10-21'"
        ).fetchone()
    assert row["scheduled_at"] == "2026-10-21 07:00:00", "publicación intacta, inicio −60m"
    assert row["target_publish_at"] == "2026-10-21 08:00:00"


# ── 9. Franja-first: N items vs F franjas ────────────────────────


def test_four_shorts_three_franjas_stay_same_day(monkeypatch):
    """4 shorts y 3 franjas: 4 slots el mismo día, uno duplicado con offset."""
    import database.db_extended as db_ext

    class _FakeDB:
        def get_optimal_slots(self, channel_id, content_type):
            return [
                {"slot_rank": 1, "target_hour": 10, "target_minute": 0},
                {"slot_rank": 2, "target_hour": 13, "target_minute": 0},
                {"slot_rank": 3, "target_hour": 18, "target_minute": 0},
            ]

        def record_slot_usage(self, *a, **k):
            return True

    monkeypatch.setattr(db_ext, "ExtendedDatabase", lambda *a, **k: _FakeDB())
    monkeypatch.setattr(ss, "_short_schedule_lead_minutes", lambda db, cid: 60)
    monkeypatch.setattr(
        "config.config_bridge.get_channel_config",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sin config")),
    )
    channel = {"id": 2, "slug": "two", "name": "Two"}
    slots, _ = ss._build_shorts_slots_for_channel(
        channel, "2026-10-18", native_count=4,
        clips_per_long=0, long_video_count=0,
        long_target_hours=[], global_start_pos=0,
    )
    assert len(slots) == 4
    assert {s_["target_publish_at"][:10] for s_ in slots} == {"2026-10-18"}
    dups = [s_ for s_ in slots if s_.get("franja_dup_index", 0) > 0]
    assert len(dups) == 1
    # El duplicado cae 3 min después del primero de su franja.
    first = next(s_ for s_ in slots if s_.get("slot_rank") == dups[0]["slot_rank"]
                 and s_.get("franja_dup_index", 0) == 0)
    delta = (
        datetime.strptime(dups[0]["target_publish_at"], "%Y-%m-%d %H:%M:%S")
        - datetime.strptime(first["target_publish_at"], "%Y-%m-%d %H:%M:%S")
    )
    assert delta.total_seconds() == 180


def test_two_shorts_three_franjas_use_max_separation(monkeypatch):
    import database.db_extended as db_ext

    class _FakeDB:
        def get_optimal_slots(self, channel_id, content_type):
            return [
                {"slot_rank": 1, "target_hour": 6, "target_minute": 0},
                {"slot_rank": 2, "target_hour": 10, "target_minute": 0},
                {"slot_rank": 3, "target_hour": 20, "target_minute": 0},
            ]

        def record_slot_usage(self, *a, **k):
            return True

    monkeypatch.setattr(db_ext, "ExtendedDatabase", lambda *a, **k: _FakeDB())
    monkeypatch.setattr(ss, "_short_schedule_lead_minutes", lambda db, cid: 60)
    monkeypatch.setattr(
        "config.config_bridge.get_channel_config",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("sin config")),
    )
    channel = {"id": 2, "slug": "two", "name": "Two"}
    slots, _ = ss._build_shorts_slots_for_channel(
        channel, "2026-10-18", native_count=2,
        clips_per_long=0, long_video_count=0,
        long_target_hours=[], global_start_pos=0,
    )
    ranks = sorted(s_["slot_rank"] for s_ in slots)
    assert ranks == [1, 3], "debe elegir 06:00 y 20:00, no 06:00 y 10:00"


def test_intentional_duplicate_allows_same_franja_execution(tmp_path, monkeypatch):
    db = _timing_db(tmp_path)
    with db._connect() as conn:
        for dup in (0, 1):
            conn.execute(
                """INSERT INTO shorts_planned_slots
                   (channel_id, date_key, scheduled_at, target_upload_at,
                    target_publish_at, short_type, status, slot_rank,
                    franja_dup_index)
                   VALUES (1, '2026-10-21', '2026-10-21 07:00:00',
                           '2026-10-21 08:00:00', '2026-10-21 08:00:00',
                           'native', 'pending', 1, ?)""",
                (dup,),
            )
        conn.commit()
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM shorts_planned_slots ORDER BY id")]
    assert ss._intentional_franja_duplicate(ids[0], db) is True
    assert ss._intentional_franja_duplicate(ids[1], db) is True


def test_gate_skips_spacing_for_intentional_duplicate(monkeypatch):
    monkeypatch.setattr(ss, "_channel_shorts_spam_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ss, "_youtube_quota_blocked", lambda *a, **k: False)
    monkeypatch.setattr(ss, "_channel_hard_daily_short_cap_reached", lambda *a, **k: False)
    monkeypatch.setattr(ss, "_global_shorts_daily_cap_reached", lambda *a, **k: False)
    monkeypatch.setattr(
        "api.services.channel_policy.resolve_channel_policy_values",
        lambda *a, **k: {"native_shorts_per_day": 3},
    )
    called = {"n": 0}

    def _spacing(*a, **k):
        called["n"] += 1
        return False, 7200

    monkeypatch.setattr(ss, "_channel_short_spacing_ok", _spacing)

    class _DB:
        def count_native_shorts_published_today(self, cid):
            return 0

    ok, reason = ss._planned_native_gate(
        1, "canal2", _DB(), allow_same_franja=True,
    )
    assert ok is True and called["n"] == 0


# ── 10. Migración v70: duplicados por franja ─────────────────────


def test_migration_v70_allows_duplicate_target_per_franja(tmp_path):
    from database.db_extended import _migrate_v70
    import logging

    path = tmp_path / "mig70.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE planned_slots (
                id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                target_public_at TEXT,
                status TEXT DEFAULT 'pending'
            );
            CREATE UNIQUE INDEX uq_active_planned_public_target
                ON planned_slots(channel_id, target_public_at)
                WHERE status IN ('pending','running') AND target_public_at IS NOT NULL;
            CREATE TABLE shorts_planned_slots (
                id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                target_publish_at TEXT
            );
        """)
        _migrate_v70(conn, logging.getLogger("test"))
        pos_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('planned_slots')")}
        sps_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('shorts_planned_slots')")}
        assert "franja_dup_index" in pos_cols
        assert "franja_dup_index" in sps_cols

        # Dos filas con el mismo target y distinto índice de duplicado.
        conn.execute(
            "INSERT INTO planned_slots (channel_id, target_public_at, status, franja_dup_index) "
            "VALUES (1, '2026-10-21 08:00:00', 'pending', 0)"
        )
        conn.execute(
            "INSERT INTO planned_slots (channel_id, target_public_at, status, franja_dup_index) "
            "VALUES (1, '2026-10-21 08:00:00', 'pending', 1)"
        )
        conn.commit()

        _migrate_v70(conn, logging.getLogger("test"))  # idempotente
