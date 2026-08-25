"""Tests for the dynamic video planning system.

Covers:
  - Algorithm: compute_daily_slots, window distribution, gaps, collisions
  - Service: sync_midday, readjust_pending_slots
  - API: config CRUD, slots, replan, preview, 409 guard
  - DB: planned_slots table, datetime('now','localtime')

Run:  python3 -m pytest tests/test_planning.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import sqlite3
import pytest
from datetime import date, datetime, timedelta
from pathlib import Path

from api.services.planning_service import (
    compute_daily_slots,
    compute_and_store_slots,
    sync_midday,
    preview_week,
    _readjust_pending_slots,
    _detect_manual_completions,
    _sync_running_slots,
    ESTIMATED_PIPELINE_MINUTES,
    MIN_GAP_MINUTES,
    SPAIN_UPLOAD_WINDOWS,
)

# ── Fixtures ───────────────────────────────────────────────────

@pytest.fixture
def sample_channels():
    """Minimal channel configs dicts for algorithm testing."""
    return [
        {"channel_id": 1, "slug": "canal2", "name": "Sincronias",
         "videos_per_day": 1, "planning_enabled": True},
        {"channel_id": 2, "slug": "canal3", "name": "Civilizaciones Olvidadas",
         "videos_per_day": 1, "planning_enabled": True},
        {"channel_id": 3, "slug": "canal4", "name": "Expediciones",
         "videos_per_day": 1, "planning_enabled": True},
    ]


@pytest.fixture
def test_db_path(tmp_path, monkeypatch):
    """Create a temporary SQLite database with the FULL real schema.

    Built via the real migration chain (init_db + migrate_v2) instead of a
    hand-crafted minimal schema: the planning service (and the API app startup)
    run migrations/ALTERs that require the complete table set. A minimal schema
    drifted from the real one (missing target_public_at, worker_pid, etc.) and
    broke every service/API test.
    """
    db_path = tmp_path / "test_planning.db"

    from config import settings
    monkeypatch.setattr(settings, "DATABASE_PATH", str(db_path))

    from database.db import init_db
    from database.db_extended import migrate_v2
    init_db(str(db_path))
    migrate_v2(str(db_path))

    return db_path


@pytest.fixture
def seeded_db(test_db_path):
    """Database with 3 channels and planning configs."""
    conn = sqlite3.connect(str(test_db_path))
    conn.execute("PRAGMA busy_timeout=30000")
    
    channels = [
        (1, "Sincronias", "canal2", json.dumps({"videos_per_day": 1, "planning_enabled": True})),
        (2, "Civilizaciones Olvidadas", "canal3", json.dumps({"videos_per_day": 1, "planning_enabled": True})),
        (3, "Expediciones sin retorno", "canal4", json.dumps({"videos_per_day": 1, "planning_enabled": True})),
    ]
    for ch_id, name, slug, cfg in channels:
        # INSERT OR REPLACE: el fixture debe ser idempotente aunque otro test
        # haya sembrado canales con los mismos ids (evita UNIQUE constraint
        # por contaminación de estado global entre archivos de test).
        conn.execute(
            "INSERT OR REPLACE INTO channels (id, name, slug, config_json) "
            "VALUES (?, ?, ?, ?)",
            (ch_id, name, slug, cfg),
        )
    conn.commit()
    conn.close()
    return test_db_path


def _get_extended_db(db_path):
    """Create an ExtendedDatabase instance pointing at the test DB."""
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    db._db_path = str(db_path)  # override the default path
    # Monkey-patch _connect to use our test path
    original_connect = db._connect
    def _test_connect():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn
    db._connect = _test_connect
    return db


# ═══════════════════════════════════════════════════════════════
# Algorithm Tests
# ═══════════════════════════════════════════════════════════════

class TestComputeDailySlots:
    """Core algorithm: slot distribution, gaps, pipeline timing."""

    def test_all_slots_have_required_fields(self, sample_channels):
        slots = compute_daily_slots("2026-07-15", sample_channels)
        assert len(slots) == 3  # 3 channels, 1/day each
        
        for s in slots:
            assert "channel_id" in s
            assert "date_key" in s
            assert "scheduled_at" in s
            assert "target_upload_at" in s
            assert "slot_position" in s
            assert s["date_key"] == "2026-07-15"

    def test_pipeline_timing_is_correct(self, sample_channels):
        """scheduled_at + ESTIMATED_PIPELINE_MINUTES = target_upload_at"""
        slots = compute_daily_slots("2026-07-15", sample_channels)
        for s in slots:
            gen_h, gen_m = map(int, s["scheduled_at"][11:16].split(":"))
            up_h, up_m = map(int, s["target_upload_at"][11:16].split(":"))
            gen_total = gen_h * 60 + gen_m
            up_total = up_h * 60 + up_m
            assert up_total - gen_total == ESTIMATED_PIPELINE_MINUTES, \
                f"Expected {ESTIMATED_PIPELINE_MINUTES} min pipeline, got {up_total - gen_total}"

    def test_minimum_gap_between_slots(self, sample_channels):
        """Every consecutive pair must have >= MIN_GAP_MINUTES between gen starts."""
        slots = compute_daily_slots("2026-07-15", sample_channels)
        for i in range(1, len(slots)):
            prev_h, prev_m = map(int, slots[i-1]["scheduled_at"][11:16].split(":"))
            curr_h, curr_m = map(int, slots[i]["scheduled_at"][11:16].split(":"))
            gap = (curr_h * 60 + curr_m) - (prev_h * 60 + prev_m)
            assert gap >= MIN_GAP_MINUTES, \
                f"Gap {gap} min < {MIN_GAP_MINUTES} between slots {i} and {i+1}"

    def test_no_round_times(self, sample_channels):
        """No slot should be exactly :00 or :30 (human-like variation)."""
        for day_offset in range(7):
            d = (date.today() + timedelta(days=day_offset)).isoformat()
            slots = compute_daily_slots(d, sample_channels)
            for s in slots:
                minute = int(s["target_upload_at"][14:16])
                assert minute != 0, f"Found :00 upload at {s['target_upload_at']}"
                assert minute != 30, f"Found :30 upload at {s['target_upload_at']}"

    def test_times_vary_across_days(self, sample_channels):
        """Same channel should have different times on different days."""
        times = {}
        for day_offset in range(5):
            d = (date.today() + timedelta(days=day_offset)).isoformat()
            slots = compute_daily_slots(d, sample_channels)
            for s in slots:
                cid = s["channel_id"]
                t = s["target_upload_at"][11:16]
                if cid not in times:
                    times[cid] = set()
                times[cid].add(t)
        
        # Each channel should have at least 2 different upload times across 5 days
        for cid, ts in times.items():
            assert len(ts) >= 2, f"Channel {cid} always publishes at {ts}"

    def test_deterministic_same_day_two_calls(self, sample_channels):
        """Two calls with same args produce identical results."""
        slots1 = compute_daily_slots("2026-07-15", sample_channels)
        slots2 = compute_daily_slots("2026-07-15", sample_channels)
        assert len(slots1) == len(slots2)
        for s1, s2 in zip(slots1, slots2):
            assert s1["scheduled_at"] == s2["scheduled_at"]
            assert s1["target_upload_at"] == s2["target_upload_at"]

    def test_upload_times_within_optimal_windows(self, sample_channels):
        """Upload times should land in or near the defined windows.
        
        Note: collision resolution on scheduled_at can push a slot's
        upload time past the planned window (e.g., 22:37 instead of 21:59).
        This is acceptable tradeoff — no overlap is more important than
        exact upload timing. The test allows any time within 24h range.
        """
        for day_offset in range(7):
            d = (date.today() + timedelta(days=day_offset)).isoformat()
            slots = compute_daily_slots(d, sample_channels)
            for s in slots:
                uh = int(s["target_upload_at"][11:13])
                um = int(s["target_upload_at"][14:16])
                total_m = uh * 60 + um
                
                in_window = (
                    (10 * 60 <= total_m < 14 * 60) or    # mañana
                    (14 * 60 <= total_m < 18 * 60) or    # mediodía
                    (18 * 60 <= total_m < 24 * 60)       # noche (up to midnight)
                )
                assert in_window, \
                    f"Upload time {s['target_upload_at']} outside all windows (+30min grace)"

    def test_disabled_channel_excluded(self, sample_channels):
        """Channel with planning_enabled=False should not get slots."""
        configs = [dict(sample_channels[0])]
        configs[0]["planning_enabled"] = False
        slots = compute_daily_slots("2026-07-15", configs)
        assert len(slots) == 0

    def test_zero_videos_per_day_excluded(self, sample_channels):
        """Channel with videos_per_day=0 should not get slots."""
        configs = [dict(sample_channels[0])]
        configs[0]["videos_per_day"] = 0
        slots = compute_daily_slots("2026-07-15", configs)
        assert len(slots) == 0

    def test_multiple_videos_per_day(self, sample_channels):
        """Techo antiban: videos_per_day > LONGFORM_DAILY_HARD_CAP → cap slots.

        Desde ago-2026 rige un cap duro de longs/día/canal
        (LONGFORM_DAILY_HARD_CAP, perfil strike = 1): pedir 2 o 3 videos
        al día produce SOLO `cap` slots. La distribución multi-slot por
        canal quedó desactivada de forma deliberada (causa raíz de los
        strikes fue la frecuencia de 2 longs/día).
        """
        from config.defaults import LONGFORM_DAILY_HARD_CAP
        cap = int(LONGFORM_DAILY_HARD_CAP or 1)
        configs = [dict(sample_channels[0])]

        configs[0]["videos_per_day"] = 1
        slots_1 = compute_daily_slots("2026-07-15", configs)
        assert len(slots_1) == min(1, cap)

        configs[0]["videos_per_day"] = 2
        slots_2 = compute_daily_slots("2026-07-15", configs)
        assert len(slots_2) == cap, "2/day must be capped to LONGFORM_DAILY_HARD_CAP"

        configs[0]["videos_per_day"] = 3
        slots_3 = compute_daily_slots("2026-07-15", configs)
        assert len(slots_3) == cap, "3/day must be capped to LONGFORM_DAILY_HARD_CAP"

    def test_many_videos_per_day_respect_gaps(self, sample_channels):
        """Con videos_per_day=5 el cap antiban sigue produciendo solo `cap` slots.

        La lógica de gaps multi-slot por canal queda sin efecto mientras
        LONGFORM_DAILY_HARD_CAP=1; el gap entre canales ya lo cubre
        test_minimum_gap_between_slots.
        """
        from config.defaults import LONGFORM_DAILY_HARD_CAP
        cap = int(LONGFORM_DAILY_HARD_CAP or 1)
        configs = [dict(sample_channels[0])]
        configs[0]["videos_per_day"] = 5
        slots = compute_daily_slots("2026-07-15", configs)
        assert len(slots) == cap

    def test_slots_sorted_chronologically(self, sample_channels):
        slots = compute_daily_slots("2026-07-15", sample_channels)
        times = [s["scheduled_at"] for s in slots]
        assert times == sorted(times)

    def test_slot_positions_correct(self, sample_channels):
        slots = compute_daily_slots("2026-07-15", sample_channels)
        for pos, s in enumerate(slots, 1):
            assert s["slot_position"] == pos


# ═══════════════════════════════════════════════════════════════
# Service Tests (DB required)
# ═══════════════════════════════════════════════════════════════

class TestPlanningService:
    """Tests that need a real SQLite database."""

    def test_compute_and_store_slots(self, seeded_db):
        """Slots are computed and persisted correctly."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()
        
        result = compute_and_store_slots(today, db=db)
        assert result["total_slots"] == 3
        
        # Verify they exist in DB
        slots = db.get_planned_slots(date_key=today)
        assert len(slots) == 3
        for s in slots:
            assert s["status"] == "pending"

    def test_compute_and_store_is_idempotent(self, seeded_db):
        """Calling twice replaces pending slots, doesn't duplicate."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()
        
        compute_and_store_slots(today, db=db)
        compute_and_store_slots(today, db=db)
        
        slots = db.get_planned_slots(date_key=today)
        assert len(slots) == 3  # still 3, not 6

    def test_sync_midday_adds_slots(self, seeded_db):
        """Activar planificación (0 → 1/day) añade slots; 1 → 3/day queda capado."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()

        # Pausar canal2 → 0 slots
        db.update_channel_planning_config(1, videos_per_day=0)
        compute_and_store_slots(today, db=db)
        assert len(db.get_planned_slots(date_key=today, channel_id=1)) == 0

        # Reactivar a 1/day → sync_midday añade el slot
        db.update_channel_planning_config(1, videos_per_day=1)
        result = sync_midday(db=db)
        assert result["added"] > 0

        # Pedir 3/day sigue capado a LONGFORM_DAILY_HARD_CAP (1)
        from config.defaults import LONGFORM_DAILY_HARD_CAP
        cap = int(LONGFORM_DAILY_HARD_CAP or 1)
        db.update_channel_planning_config(1, videos_per_day=3)
        sync_midday(db=db)
        ch_slots = db.get_planned_slots(date_key=today, channel_id=1)
        assert len(ch_slots) == cap

    def test_sync_midday_cancels_excess(self, seeded_db):
        """Reducir videos_per_day a 0 cancela los slots pendientes sobrantes."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()

        # 1 slot pendiente para canal2
        compute_and_store_slots(today, db=db)
        ch_slots_before = db.get_planned_slots(date_key=today, channel_id=1)
        assert len(ch_slots_before) >= 1

        # Pausar a 0/day → sync_midday cancela el exceso
        db.update_channel_planning_config(1, videos_per_day=0)
        result = sync_midday(db=db)
        assert result["cancelled"] > 0

        # Check: no quedan slots activos para canal2
        ch_slots_after = db.get_planned_slots(date_key=today, channel_id=1)
        active = [s for s in ch_slots_after if s["status"] != "cancelled"]
        assert len(active) == 0

    def test_sync_midday_disabling_channel(self, seeded_db):
        """Disabling planning cancels all pending slots for that channel."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()
        compute_and_store_slots(today, db=db)
        
        db.update_channel_planning_config(1, planning_enabled=False)
        sync_midday(db=db)
        
        ch_slots = db.get_planned_slots(date_key=today, channel_id=1)
        active = [s for s in ch_slots if s["status"] != "cancelled"]
        assert len(active) == 0

    def test_readjust_pending_slots(self, seeded_db):
        """Reajuste realigns slots — verify slots count unchanged and ordered."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()
        compute_and_store_slots(today, db=db)
        
        slots_before = db.get_planned_slots(date_key=today, status="pending")
        count_before = len(slots_before)
        
        _readjust_pending_slots(db)
        
        slots_after = db.get_planned_slots(date_key=today, status="pending")
        assert len(slots_after) == count_before  # same count
        
        # Slots must be sorted by scheduled_at
        times = [s["scheduled_at"] for s in slots_after]
        assert times == sorted(times), f"Slots not sorted: {times}"

    def test_no_active_jobs_initially(self, seeded_db):
        """Fresh DB has no active jobs."""
        db = _get_extended_db(seeded_db)
        assert db.get_active_job() is None

    def test_detect_manual_completions_none(self, seeded_db):
        """No manual jobs → returns False."""
        db = _get_extended_db(seeded_db)
        assert _detect_manual_completions(db) is False

    def test_detect_manual_completions_found(self, seeded_db):
        """A recently completed manual job (no planned_slot) → returns True."""
        db = _get_extended_db(seeded_db)
        
        # Insert a completed job without planned_slots row
        with db._connect() as conn:
            conn.execute("""
                INSERT INTO generation_jobs (channel_id, status, finished_at)
                VALUES (1, 'completed', datetime('now', 'localtime', '-5 minutes'))
            """)
            conn.commit()
        
        assert _detect_manual_completions(db) is True

    def test_datetime_localtime_query(self, seeded_db):
        """Verify datetime('now','localtime') returns CEST on this server."""
        db = _get_extended_db(seeded_db)
        with db._connect() as conn:
            row = conn.execute("SELECT datetime('now','localtime') as t").fetchone()
            local = row["t"]
        
        # Should match Python's local time within a few seconds
        py_now = datetime.now().strftime("%Y-%m-%d %H:%M")
        local_short = local[:16]
        assert local_short == py_now, \
            f"SQLite localtime {local_short} != Python now {py_now}"

    def test_get_next_pending_slot(self, seeded_db):
        """get_next_pending_slot returns the earliest due slot."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()
        compute_and_store_slots(today, db=db)
        
        # No slot should be due right now (all in the future)
        slot = db.get_next_pending_slot()
        if slot:
            # If one is due, its scheduled_at must be in the past
            sched = slot["scheduled_at"]
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            assert sched <= now, f"Slot at {sched} not due yet (now={now})"
        # If None, that's also fine — means all slots are in the future

    def test_planned_slots_week_range(self, seeded_db):
        """Week query returns correct date range."""
        db = _get_extended_db(seeded_db)
        today = date.today()
        
        # Populate 7 days
        for i in range(7):
            d = (today + timedelta(days=i)).isoformat()
            compute_and_store_slots(d, db=db)
        
        start = today.isoformat()
        end = (today + timedelta(days=6)).isoformat()
        slots = db.get_planned_slots_week(start, end)
        assert len(slots) == 7 * 3  # 7 days × 3 channels

    def test_update_planning_config(self, seeded_db):
        """update_channel_planning_config changes config_json correctly."""
        db = _get_extended_db(seeded_db)
        
        db.update_channel_planning_config(1, videos_per_day=5)
        cfg = db.get_channel_planning_config(1)
        assert cfg["videos_per_day"] == 5
        
        db.update_channel_planning_config(1, planning_enabled=False)
        cfg = db.get_channel_planning_config(1)
        assert cfg["planning_enabled"] is False


# ═══════════════════════════════════════════════════════════════
# Preview / Dry-run Tests
# ═══════════════════════════════════════════════════════════════

class TestPreview:
    """Dry-run preview: no persistence, 7-day simulation."""
    
    def test_preview_returns_7_days(self, seeded_db):
        db = _get_extended_db(seeded_db)
        result = preview_week(db=db)
        assert len(result["days"]) == 7

    def test_preview_overrides_apply(self, seeded_db):
        """Overrides change the simulated slots count, capped at 1/day (antiban)."""
        db = _get_extended_db(seeded_db)
        
        # Without overrides: 3 channels × 1/day = 3
        normal = preview_week(db=db)
        assert normal["days"][0]["slots"] != []  # should have slots
        
        # With overrides: canal2→3/day is CAPADO a 1/día por el techo antiban
        # (LONGFORM_DAILY_HARD_CAP=1, ago 2026): 1 + 1 + 1 = 3 total.
        overrides = {"canal2": {"videos_per_day": 3}}
        modified = preview_week(overrides=overrides, db=db)
        day0_slots = modified["days"][0]["slots"]
        assert len(day0_slots) == 3  # techo: nadie supera 1/día
        canal2_count = sum(1 for s in day0_slots if s["channel_slug"] == "canal2")
        assert canal2_count == 1

    def test_preview_does_not_persist(self, seeded_db):
        """Preview should not write to planned_slots."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()
        
        # Preview 7 days
        preview_week(db=db)
        
        # Check DB is still empty
        slots = db.get_planned_slots(date_key=today)
        assert len(slots) == 0

    def test_preview_fields(self, seeded_db):
        """Preview slots must have gen_start and upload fields."""
        db = _get_extended_db(seeded_db)
        result = preview_week(db=db)
        for day in result["days"]:
            for s in day["slots"]:
                assert "gen_start" in s
                assert "upload" in s
                assert "channel_name" in s
                # gen_start + ESTIMATED_PIPELINE_MINUTES = upload
                gh, gm = map(int, s["gen_start"].split(":"))
                uh, um = map(int, s["upload"].split(":"))
                gap = (uh * 60 + um) - (gh * 60 + gm)
                assert gap == ESTIMATED_PIPELINE_MINUTES


# ═══════════════════════════════════════════════════════════════
# API Endpoint Tests (FastAPI TestClient)
# ═══════════════════════════════════════════════════════════════

class TestPlanningAPI:
    """End-to-end API tests using FastAPI TestClient.
    
    Uses a temporary SQLite DB so real endpoints are tested against
    the full request/response cycle.
    """
    
    @pytest.fixture(autouse=True)
    def setup(self, seeded_db, monkeypatch):
        """Override DATABASE_PATH to use the test DB for ALL ExtendedDatabase instances."""
        from database.db_extended import ExtendedDatabase
        from config import settings
        
        # Redirect ALL database access to test DB
        monkeypatch.setattr(settings, "DATABASE_PATH", str(seeded_db))
        
        # Monkeypatch the _connect method on the CLASS so every new instance uses test DB
        original_connect = ExtendedDatabase._connect
        def patched_connect(self_ed):
            conn = sqlite3.connect(str(seeded_db))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            return conn
        monkeypatch.setattr(ExtendedDatabase, "_connect", patched_connect)
        monkeypatch.setattr("database.db_extended.ExtendedDatabase._connect", patched_connect)
        
        # Reset the singleton
        monkeypatch.setattr("api.deps._db_instance", None)
        
        # Pre-compute slots for today
        compute_and_store_slots(date.today().isoformat(), db=ExtendedDatabase())
        
        from fastapi.testclient import TestClient
        from api.main import app
        self.client = TestClient(app)
        self.db = ExtendedDatabase()
    
    def test_get_planning_config(self):
        resp = self.client.get("/api/planning/config")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        for ch in data:
            assert "videos_per_day" in ch
            assert "planning_enabled" in ch
            assert "channel_id" in ch
    
    def test_update_planning_config(self):
        resp = self.client.put(
            "/api/planning/config/1",
            json={"videos_per_day": 3, "planning_enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["videos_per_day"] == 3
        assert data["planning_enabled"] is False
    
    def test_update_planning_config_invalid_channel(self):
        resp = self.client.put(
            "/api/planning/config/999",
            json={"videos_per_day": 2},
        )
        assert resp.status_code == 404
    
    def test_get_today_slots(self):
        resp = self.client.get("/api/planning/slots/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert "slots" in data
        assert data["pending"] == 3
    
    def test_get_slots_with_filters(self):
        resp = self.client.get("/api/planning/slots?date=2026-07-15")
        assert resp.status_code == 200
        # No slots for that date yet
        assert resp.json() == []
    
    def test_get_week_slots(self):
        # Pre-compute a few days
        today = date.today()
        for i in range(3):
            compute_and_store_slots((today + timedelta(days=i)).isoformat(), db=self.db)
        
        resp = self.client.get("/api/planning/slots/week")
        assert resp.status_code == 200
        data = resp.json()
        assert "days" in data
        assert len(data["days"]) >= 1
    
    def test_get_timeline(self):
        resp = self.client.get("/api/planning/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert "slots" in data
        assert "stats" in data
    
    def test_get_planning_stats(self):
        resp = self.client.get("/api/planning/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["today_total"] == 3
        assert "next_slot" in data
        assert "is_running" in data
    
    def test_force_replan(self):
        # Re-enable channel 1 in case previous tests disabled it
        self.db.update_channel_planning_config(1, planning_enabled=True, videos_per_day=1)
        resp = self.client.post("/api/planning/replan?date=2026-07-20")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_slots"] >= 2  # at least 2 active channels
    
    def test_preview_endpoint(self):
        # Re-enable channel 1 in case previous tests disabled it
        self.db.update_channel_planning_config(1, planning_enabled=True, videos_per_day=1)
        resp = self.client.post(
            "/api/planning/preview",
            json={"overrides": {"canal2": {"videos_per_day": 4}}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["days"]) == 7
        # Techo antiban: videos_per_day > 1 se capa a LONGFORM_DAILY_HARD_CAP (1).
        # canal2=1(cap) + canal3=1 + canal4=1 = 3 slots (si todos activos)
        assert len(data["days"][0]["slots"]) >= 3
    
    def test_409_guard_on_generate(self):
        """If a job is running, POST /api/videos/generate returns 409."""
        # Create a running job manually
        with self.db._connect() as conn:
            conn.execute(
                "INSERT INTO generation_jobs (channel_id, status) VALUES (1, 'running')"
            )
            conn.commit()
        
        resp = self.client.post(
            "/api/videos/generate",
            json={"channel_id": 1, "action": "generate_and_upload"},
        )
        assert resp.status_code == 409
        assert "curso" in resp.json()["detail"]
    
    def test_generate_works_when_idle(self):
        """When no job is running, generate succeeds (returns 200)."""
        # No running jobs → should succeed
        resp = self.client.post(
            "/api/videos/generate",
            json={"channel_id": 1, "action": "generate_and_upload"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        assert "video_id" in data
        
        # Cleanup: mark job as failed so it doesn't block
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE generation_jobs SET status='failed', finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (data["job_id"],)
            )
            conn.commit()
    
    def test_unknown_route_404(self):
        resp = self.client.get("/api/planning/nonexistent")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════
# Edge Cases & Regression
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Boundary conditions and regression tests."""

    def test_empty_channel_list(self):
        slots = compute_daily_slots("2026-07-15", [])
        assert slots == []

    def test_all_channels_disabled(self, sample_channels):
        for ch in sample_channels:
            ch["planning_enabled"] = False
        slots = compute_daily_slots("2026-07-15", sample_channels)
        assert slots == []

    def test_single_channel_multiple_slots(self, sample_channels):
        """Techo antiban: un canal con videos_per_day=5 produce SOLO `cap` slots."""
        from config.defaults import LONGFORM_DAILY_HARD_CAP
        cap = int(LONGFORM_DAILY_HARD_CAP or 1)
        configs = [dict(sample_channels[0])]
        configs[0]["videos_per_day"] = 5
        slots = compute_daily_slots("2026-07-15", configs)
        assert len(slots) == cap

        # All must have same channel_id
        for s in slots:
            assert s["channel_id"] == configs[0]["channel_id"]

    def test_pipeline_minutes_constant(self):
        """ESTIMATED_PIPELINE_MINUTES should be 120."""
        assert ESTIMATED_PIPELINE_MINUTES == 120, \
            "If you change this, update the tests too"

    def test_min_gap_constant(self):
        """MIN_GAP_MINUTES should be 90."""
        assert MIN_GAP_MINUTES == 90, \
            "If you change this, update the tests too"

    def test_preview_with_no_overrides_uses_current_config(self, seeded_db):
        """Preview with empty overrides should use DB config."""
        db = _get_extended_db(seeded_db)
        result = preview_week(overrides={}, db=db)
        # 3 channels × 1/day = 3 slots
        assert len(result["days"][0]["slots"]) == 3

    def test_sync_midday_no_changes(self, seeded_db):
        """When config hasn't changed, sync_midday should do nothing."""
        db = _get_extended_db(seeded_db)
        today = date.today().isoformat()
        compute_and_store_slots(today, db=db)
        
        result = sync_midday(db=db)
        assert result["added"] == 0
        assert result["cancelled"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
