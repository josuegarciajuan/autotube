"""Tests for quota-aware planning (ago 2026).

Cubre:
  - Presupuesto automático por proyecto GCP derivado de settings (sin hardcode).
  - Reparto del cupo (~6 subidas/día/proyecto) entre canales: longs → clips → nativos.
  - compute_horizon_slots / compute_daily_slots respetan el cupo.
  - compute_daily_shorts_slots recorta nativos al cupo del proyecto.
  - El cupo restante de hoy descuenta reservas (yt_quota_reservations) y log.
  - Batching de subidas por cuenta (mañana/tarde) con upload+warmup <= publish.
  - _cancel_excess_pending_by_quota sin dejar slots huérfanos.
  - No se dispara el breaker por exceso de planificación.

Run:  python3 -m pytest tests/test_quota_aware_planning.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import sqlite3
import pytest
from datetime import date, timedelta

import config.settings as settings
from api.services import quota_tracker
from api.services.quota_tracker import (
    get_project_max_daily_uploads,
    get_project_remaining_upload_slots,
    get_project_used_upload_units,
)
from api.services.planning_service import (
    compute_daily_upload_allocation,
    compute_horizon_slots,
    compute_daily_slots,
    _cancel_excess_pending_by_quota,
)

PROJECT_A = "youtube-uploads-automation"
PROJECT_B = "autotube-expediciones"

# (channel_id, name, slug, project, google_account)
CHANNELS = [
    (3, "Sincronías", "canal2", PROJECT_A, "tracatrack"),
    (4, "Civilizaciones Olvidadas", "canal3", PROJECT_A, "tracatrack"),
    (5, "Expediciones sin retorno", "canal4", PROJECT_B, "burrianacasa2026"),
    (7, "Anomalías Médicas", "canal5", PROJECT_B, "burrianacasa2026"),
]


@pytest.fixture
def quota_env(monkeypatch):
    """Presupuesto por proyecto 10000 − reservados 400 = 9600 → 6 subidas/día."""
    monkeypatch.setattr(settings, "YT_PROJECT_BUDGET_UNITS", {
        PROJECT_A: 10000, PROJECT_B: 10000,
    })
    monkeypatch.setattr(settings, "YT_PROJECT_RESERVED_UNITS", 400)
    quota_tracker._project_cache.clear()
    for _ch_id, _name, slug, proj, _acc in CHANNELS:
        quota_tracker._project_cache[slug] = proj
    yield
    quota_tracker._project_cache.clear()


@pytest.fixture
def quota_db(tmp_path):
    """Base de datos con schema mínimo (tablas que toca el planificador) + 4 canales."""
    db_path = tmp_path / "quota_planning.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            config_json TEXT NOT NULL DEFAULT '{}',
            active BOOLEAN NOT NULL DEFAULT 1,
            google_account TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE planned_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            date_key TEXT NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            target_upload_at TIMESTAMP,
            target_public_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'pending',
            slot_position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE shorts_planned_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            date_key TEXT NOT NULL,
            scheduled_at TIMESTAMP NOT NULL,
            target_upload_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'pending',
            short_type TEXT NOT NULL DEFAULT 'native',
            slot_position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE shorts_planning_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            shorts_enabled INTEGER DEFAULT 1,
            shorts_native_per_day INTEGER DEFAULT 3,
            shorts_clip_per_day INTEGER DEFAULT 1,
            shorts_clips_per_long INTEGER DEFAULT 1,
            shorts_per_day INTEGER DEFAULT 3,
            shorts_native_ratio REAL DEFAULT 0.35,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE yt_quota_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            quota_day_pt TEXT NOT NULL,
            operation TEXT NOT NULL,
            content_class TEXT NOT NULL,
            units INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved',
            reference_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finalized_at TEXT
        );
        CREATE TABLE yt_quota_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            channel_slug TEXT NOT NULL,
            operation TEXT NOT NULL,
            units INTEGER NOT NULL,
            yt_id TEXT DEFAULT '',
            success INTEGER DEFAULT 1,
            error TEXT DEFAULT '',
            caller TEXT DEFAULT ''
        );
        CREATE TABLE pipeline_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT,
            entity_id INTEGER,
            channel_id INTEGER,
            alert_type TEXT,
            severity TEXT DEFAULT 'warning',
            title TEXT,
            message TEXT,
            resolved BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    for ch_id, name, slug, _proj, acc in CHANNELS:
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active, google_account) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (ch_id, name, slug,
             json.dumps({"videos_per_day": 2, "planning_enabled": True}), acc),
        )
        conn.execute(
            """INSERT INTO shorts_planning_config
               (channel_id, shorts_enabled, shorts_native_per_day, shorts_clip_per_day,
                shorts_clips_per_long, shorts_per_day, shorts_native_ratio)
               VALUES (?, 1, 2, 1, 1, 2, 0.5)""",
            (ch_id,),
        )
    conn.commit()
    conn.close()

    from database.db_extended import ExtendedDatabase
    return ExtendedDatabase(str(db_path))


def _day(future_days: int = 3) -> str:
    return (date.today() + timedelta(days=future_days)).isoformat()


def _channel_configs(db):
    """Configs de planificación long-form para los 4 canales (scheduled)."""
    configs = []
    for ch_id, _name, slug, _proj, _acc in CHANNELS:
        cfg = db.get_channel_planning_config(ch_id)
        cfg["publish_mode"] = "scheduled"
        cfg["publish_warmup_min"] = 120
        cfg["publish_timezone"] = "Europe/Madrid"
        cfg["generation_lead_hours"] = 36
        cfg["avg_duration_min"] = 120
        cfg["slug"] = slug
        configs.append(cfg)
    return configs


# ═══════════════════════════════════════════════════════════════
# 1. Presupuesto derivado de settings (sin hardcode)
# ═══════════════════════════════════════════════════════════════

class TestBudgetDerivation:
    def test_automatic_budget_9600(self, quota_env):
        from config.settings import get_project_automatic_budget_units
        assert get_project_automatic_budget_units(PROJECT_A) == 9600
        assert get_project_automatic_budget_units(PROJECT_B) == 9600

    def test_max_daily_uploads_is_six(self, quota_env):
        assert get_project_max_daily_uploads(PROJECT_A) == 6
        assert get_project_max_daily_uploads(PROJECT_B) == 6

    def test_remaining_slots_fresh_day(self, quota_env, quota_db):
        assert get_project_remaining_upload_slots(PROJECT_A, db=quota_db) == 6
        assert get_project_used_upload_units(PROJECT_A, db=quota_db) == 0


# ═══════════════════════════════════════════════════════════════
# 2. Reparto del cupo entre canales (longs → clips → nativos)
# ═══════════════════════════════════════════════════════════════

class TestAllocation:
    def test_future_day_caps_per_project(self, quota_env, quota_db):
        """Techo antiban: 1 long/canal/día; cupo 6 por proyecto.

        Invariantes (ago 2026, LONGFORM_DAILY_HARD_CAP=1):
          - longs = 2 por proyecto (1/canal × 2 canales), NUNCA más
          - longs + nativos (subidas planificadas) <= max_uploads
          - los clips (1 por long) se reservan antes que los nativos
          - con longs+clips < cupo, quedan nativos hasta llenar el cupo
        """
        alloc = compute_daily_upload_allocation(quota_db, _day(3))
        for proj in (PROJECT_A, PROJECT_B):
            data = alloc[proj]
            chans = data["channels"]
            longs = sum(c["long"] for c in chans.values())
            natives = sum(c["native"] for c in chans.values())
            clips = sum(c["clips_est"] for c in chans.values())
            # Subidas planificadas dentro del cupo
            assert longs + natives <= data["max_uploads"]
            # Techo antiban: 1 long/canal → 2 longs por proyecto (no 4)
            assert longs == 2
            # Clips = 1 por long planificado
            assert clips >= longs
            # Cupo sobrante (6 - 2 longs - 2 clips = 2) permite nativos
            assert natives >= 1

    def test_allocation_round_robin_fairness(self, quota_env, quota_db):
        """Con vpd=1 por canal, los longs se reparten equitativamente."""
        with quota_db._connect() as conn:
            for ch_id, _n, _s, _p, _a in CHANNELS:
                conn.execute(
                    "UPDATE channels SET config_json = ? WHERE id = ?",
                    (json.dumps({"videos_per_day": 1, "planning_enabled": True}), ch_id),
                )
            conn.commit()
        alloc = compute_daily_upload_allocation(quota_db, _day(2))
        for proj in (PROJECT_A, PROJECT_B):
            chans = alloc[proj]["channels"]
            long_caps = [c["long"] for c in chans.values()]
            native_caps = [c["native"] for c in chans.values()]
            # Round-robin: las diferencias entre canales son <= 1
            assert max(long_caps) - min(long_caps) <= 1
            assert max(native_caps) - min(native_caps) <= 1
            # Cupo respetado
            assert sum(long_caps) + sum(native_caps) <= alloc[proj]["max_uploads"]


# ═══════════════════════════════════════════════════════════════
# 3. Planificación long-form respeta el cupo
# ═══════════════════════════════════════════════════════════════

class TestHorizonSlotsCap:
    def test_horizon_never_plans_more_than_cap(self, quota_env, quota_db):
        slots = compute_horizon_slots(_channel_configs(quota_db),
                                      horizon_days=7, db=quota_db)
        assert slots
        by_proj_day = {}
        for s in slots:
            ch = {ch[2]: ch[3] for ch in CHANNELS}[s["channel_slug"]]
            by_proj_day.setdefault((ch, s["date_key"]), 0)
            by_proj_day[(ch, s["date_key"])] += 1
        for (proj, day_key), count in by_proj_day.items():
            assert count <= 6, f"{proj} {day_key}: {count} longs > 6"
            # Longs planificados == longs asignados por la allocation
            alloc = compute_daily_upload_allocation(quota_db, day_key)
            expected = sum(c["long"] for c in alloc[proj]["channels"].values())
            assert count == expected

    def test_compute_daily_slots_respects_cap(self, quota_env, quota_db):
        configs = _channel_configs(quota_db)
        slots = compute_daily_slots(_day(1), configs, db=quota_db)
        # El cupo es POR PROYECTO: cada proyecto no planifica más de 6 longs
        by_proj = {}
        for s in slots:
            proj = {ch[2]: ch[3] for ch in CHANNELS}[s["channel_slug"]]
            by_proj[proj] = by_proj.get(proj, 0) + 1
        for proj, count in by_proj.items():
            assert count <= 6, f"{proj}: {count} longs > cupo 6"
        # Total = suma de los longs asignados por la allocation
        alloc = compute_daily_upload_allocation(quota_db, _day(1))
        expected = sum(
            sum(c["long"] for c in alloc[proj]["channels"].values())
            for proj in alloc
        )
        assert len(slots) == expected


# ═══════════════════════════════════════════════════════════════
# 4. Shorts nativos recortados al cupo del proyecto
# ═══════════════════════════════════════════════════════════════

class TestShortsCap:
    def test_native_shorts_capped_by_project(self, quota_env, quota_db, monkeypatch):
        from api.services import shorts_scheduler
        # vpd=1 → 2 longs + 2 clips → quedan 2 nativos (1 por canal)
        with quota_db._connect() as conn:
            for ch_id, _n, _s, _p, _a in CHANNELS:
                conn.execute(
                    "UPDATE channels SET config_json = ? WHERE id = ?",
                    (json.dumps({"videos_per_day": 1, "planning_enabled": True}), ch_id),
                )
            conn.commit()
        # Evitar dependencias externas del reparto adaptativo
        monkeypatch.setattr(shorts_scheduler, "get_shorts_distribution",
                            lambda ch_id, db, date_str: (2, 0))
        monkeypatch.setattr(shorts_scheduler, "_get_yesterday_published_count",
                            lambda ch_id, date_str, db=None: 0)
        slots = shorts_scheduler.compute_daily_shorts_slots(_day(2), db=quota_db)
        per_channel = {}
        for s in slots:
            per_channel[s["channel_id"]] = per_channel.get(s["channel_id"], 0) + 1
        for ch_id in (3, 4, 5, 7):
            assert per_channel.get(ch_id, 0) <= 1, f"ch{ch_id}: natives > cap"

    def test_native_shorts_zero_when_cupo_agotado(self, quota_env, quota_db, monkeypatch):
        """Cupo del proyecto agotado por longs+clips → 0 nativos planificados."""
        from api.services import shorts_scheduler
        # Techo antiban: 2 longs (1/canal) × 3 clips/long = 8 subidas > cupo 6
        # → los nativos (prioridad más baja) se quedan a 0.
        with quota_db._connect() as conn:
            conn.execute("UPDATE shorts_planning_config SET shorts_clips_per_long = 3")
            conn.commit()
        monkeypatch.setattr(shorts_scheduler, "get_shorts_distribution",
                            lambda ch_id, db, date_str: (2, 0))
        monkeypatch.setattr(shorts_scheduler, "_get_yesterday_published_count",
                            lambda ch_id, date_str, db=None: 0)
        slots = shorts_scheduler.compute_daily_shorts_slots(_day(2), db=quota_db)
        assert slots == []


# ═══════════════════════════════════════════════════════════════
# 5. Cupo restante de hoy descuenta reservas
# ═══════════════════════════════════════════════════════════════

class TestRemainingBudgetToday:
    def test_reservations_reduce_today_capacity(self, quota_env, quota_db):
        from api.services.quota_tracker import quota_day_pacific
        today_pt = quota_day_pacific()
        with quota_db._connect() as conn:
            for i in range(2):
                conn.execute(
                    """INSERT INTO yt_quota_reservations
                       (project_id, quota_day_pt, operation, content_class, units,
                        status, reference_id)
                       VALUES (?, ?, 'videos.insert', 'long', 1600, 'consumed', ?)""",
                    (PROJECT_A, today_pt, f"upload:{i}"),
                )
            conn.commit()
        used = get_project_used_upload_units(PROJECT_A, db=quota_db)
        assert used == 3200
        remaining = get_project_remaining_upload_slots(PROJECT_A, db=quota_db)
        assert remaining == 4  # (9600-3200)//1600
        # Hoy: allocation para el proyecto A <= 4
        alloc = compute_daily_upload_allocation(quota_db, date.today().isoformat())
        data = alloc[PROJECT_A]
        planned = sum(c["long"] + c["native"] for c in data["channels"].values())
        assert planned <= 4

    def test_no_breaker_on_planned_capacity(self, quota_env, quota_db):
        """Planificar el cupo completo no reserva ni dispara el breaker."""
        slots = compute_horizon_slots(_channel_configs(quota_db),
                                      horizon_days=7, db=quota_db)
        # Reservas simuladas por cada slot planificado, agrupado por proyecto y DÍA
        # (la cuota se resetea cada día-PT: nunca más de 6 subidas/día/proyecto).
        used_by_proj_day = {}
        for s in slots:
            proj = {ch[2]: ch[3] for ch in CHANNELS}[s["channel_slug"]]
            used_by_proj_day[(proj, s["date_key"])] = \
                used_by_proj_day.get((proj, s["date_key"]), 0) + 1600
        for (proj, day_key), used in used_by_proj_day.items():
            assert used <= 9600, f"{proj} {day_key} planificaría {used} ud > 9600"
        # La planificación NO reserva cuota (solo crea slots): reservas = 0
        for proj in (PROJECT_A, PROJECT_B):
            assert get_project_used_upload_units(proj, db=quota_db) == 0
            remaining = get_project_remaining_upload_slots(proj, db=quota_db)
            assert remaining >= 0


# ═══════════════════════════════════════════════════════════════
# 6. Batching de subidas por cuenta
# ═══════════════════════════════════════════════════════════════

class TestUploadBatching:
    def test_same_account_slots_land_in_batch_times(self, quota_env, quota_db):
        from zoneinfo import ZoneInfo
        slots = compute_horizon_slots(_channel_configs(quota_db),
                                      horizon_days=7, db=quota_db)
        # Todos los slots de una misma cuenta y día → 09:30 o 16:00 (±5 min)
        batch_minutes = {9 * 60 + 30, 16 * 60}
        by_account_day = {}
        for s in slots:
            acc = {ch[2]: ch[4] for ch in CHANNELS}[s["channel_slug"]]
            by_account_day.setdefault((acc, s["date_key"]), []).append(s)
        tz = ZoneInfo("Europe/Madrid")
        from datetime import datetime as dt
        for (acc, day_key), group in by_account_day.items():
            for s in group:
                tu = str(s["target_upload_at"])[11:16]
                h, m = int(tu[:2]), int(tu[3:])
                total = h * 60 + m
                near = any(abs(total - b) <= 5 for b in batch_minutes)
                assert near, f"{acc} {day_key} {s['channel_slug']}: upload {tu} fuera de batch"
                # upload(local) + warmup(120) <= publish(UTC→local)
                up_local = dt.strptime(
                    str(s["target_upload_at"])[:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=tz)
                pub_utc = dt.fromisoformat(
                    str(s["target_public_at"]).replace("Z", "+00:00")
                )
                pub_local = pub_utc.astimezone(tz)
                assert up_local + timedelta(minutes=120) <= pub_local, \
                    f"{s['channel_slug']}: upload {up_local:%H:%M} + warmup > publish {pub_local:%H:%M}"

    def test_batching_two_batches_per_day(self, quota_env, quota_db):
        """Con varios longs por proyecto (vpd=2), los uploads se reparten mañana/tarde."""
        slots = compute_horizon_slots(_channel_configs(quota_db),
                                      horizon_days=7, db=quota_db)
        by_account_day = {}
        for s in slots:
            acc = {ch[2]: ch[4] for ch in CHANNELS}[s["channel_slug"]]
            by_account_day.setdefault((acc, s["date_key"]), []).append(s)
        for (acc, day_key), group in by_account_day.items():
            hours = sorted({str(s["target_upload_at"])[11:13] for s in group})
            # Los uploads del día solo caen en horas de batch (09 o 16)
            assert set(hours) <= {"09", "16"}, \
                f"{acc} {day_key}: horas {hours} fuera de batch"
            # Con >=4 subidas el día se reparte entre mañana y tarde
            if len(group) >= 4:
                morning = sum(1 for s in group if str(s["target_upload_at"])[11:13] == "09")
                afternoon = sum(1 for s in group if str(s["target_upload_at"])[11:13] == "16")
                assert morning > 0 and afternoon > 0, \
                    f"{acc} {day_key}: sin reparto mañana/tarde ({morning}/{afternoon})"


# ═══════════════════════════════════════════════════════════════
# 7. Cancelación de pendientes que exceden el cupo
# ═══════════════════════════════════════════════════════════════

class TestCancelExcessPending:
    def test_cancels_shorts_first_then_longs(self, quota_env, quota_db):
        today = date.today().isoformat()
        # Insertar 4 longs + 4 shorts pendientes para proyecto A (cupo 6)
        with quota_db._connect() as conn:
            for ch_id in (3, 4):
                for pos in (1, 2):
                    conn.execute(
                        """INSERT INTO planned_slots (channel_id, date_key, scheduled_at,
                           target_upload_at, status, slot_position)
                           VALUES (?, ?, datetime('now'), datetime('now'), 'pending', ?)""",
                        (ch_id, today, pos),
                    )
                    conn.execute(
                        """INSERT INTO shorts_planned_slots (channel_id, date_key, scheduled_at,
                           target_upload_at, status, short_type, slot_position)
                           VALUES (?, ?, datetime('now'), datetime('now'), 'pending', 'native', ?)""",
                        (ch_id, today, pos),
                    )
            conn.commit()
        cancelled = _cancel_excess_pending_by_quota(quota_db)
        assert cancelled == 2  # exceso: 8 pendientes − 6 cupo
        with quota_db._connect() as conn:
            longs = conn.execute(
                "SELECT COUNT(*) c FROM planned_slots WHERE status='pending' AND date_key=?",
                (today,),
            ).fetchone()["c"]
            shorts = conn.execute(
                "SELECT COUNT(*) c FROM shorts_planned_slots WHERE status='pending' AND date_key=?",
                (today,),
            ).fetchone()["c"]
        # Los shorts se cancelan primero (prioridad baja)
        assert longs == 4
        assert shorts == 2
        assert longs + shorts <= 6
