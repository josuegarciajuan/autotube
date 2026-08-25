"""Tests for the v40 persisted 24h horizon-replan gate and marathon cooldown.

Cubre:
  - get_last_replan_ts: fallback a now-24h cuando la clave no existe; devuelve
    el valor almacenado cuando existe.
  - compute_and_store_horizon persiste la clave `last_horizon_replan_ts` cuando
    realmente ejecuta.
  - Marathon cooldown: select_marathon_channel salta un canal cuyo
    get_last_marathon es reciente (< MARATHON_COOLDOWN_HOURS) y elige otro
    (o ninguno si todos están en cooldown).

Run:  python3 -m pytest tests/test_replan_24h_gate.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import sqlite3
import pytest
from datetime import datetime, timedelta

from database.db_extended import ExtendedDatabase

# (channel_id, name, slug, project, google_account)
CHANNELS = [
    (3, "Sincronías", "canal2", "youtube-uploads-automation", "tracatrack"),
    (4, "Civilizaciones Olvidadas", "canal3", "youtube-uploads-automation", "tracatrack"),
    (5, "Expediciones sin retorno", "canal4", "autotube-expediciones", "burrianacasa2026"),
]


@pytest.fixture
def db(tmp_path):
    """Base de datos con schema mínimo (incluye system_state) + 3 canales."""
    db_path = tmp_path / "replan_gate.db"
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
            upload_window_start INTEGER,
            upload_window_end INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            slot_position INTEGER DEFAULT 0,
            source_mode TEXT DEFAULT 'original',
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
             json.dumps({
                 "videos_per_day": 1,
                 "planning_enabled": True,
                 "MARATHON_ENABLED": True,
                 # publicacion programada → target_public_at se calcula
                 "PUBLISH_MODE": "scheduled",
                 "PUBLISH_TARGET_HOUR": 18,
                 "PUBLISH_TIMEZONE": "Europe/Madrid",
                 "PUBLISH_WARMUP_MIN": 120,
             }), acc),
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
    return ExtendedDatabase(str(db_path))


@pytest.fixture(autouse=True)
def reset_module_state():
    """Resetea el estado en memoria del planning_service entre tests.

    _last_horizon_replan_ts y _post_full_replan_block_until son globals de
    módulo que sobreviven entre tests en el mismo proceso.
    """
    import api.services.planning_service as ps
    ps._last_horizon_replan_ts = None
    ps._post_full_replan_block_until = None
    yield
    ps._last_horizon_replan_ts = None
    ps._post_full_replan_block_until = None


# ═══════════════════════════════════════════════════════════════
# 1. get_last_replan_ts
# ═══════════════════════════════════════════════════════════════

class TestGetLastReplanTs:
    def test_fallback_to_now_minus_24h_when_key_missing(self, db):
        """Sin clave almacenada → fallback a now - 24h (el primer check dispara)."""
        import time
        from api.services.planning_service import get_last_replan_ts
        ts = get_last_replan_ts(db)
        expected = time.time() - 24 * 3600
        assert abs(ts - expected) < 10  # tolerancia segundos

    def test_returns_stored_value_when_key_exists(self, db):
        """Con clave almacenada → devuelve el valor persistido."""
        import time
        from api.services.planning_service import get_last_replan_ts
        stored = time.time() - 3600  # hace 1h
        db.set_system_state("last_horizon_replan_ts", str(stored))
        ts = get_last_replan_ts(db)
        assert ts == stored


# ═══════════════════════════════════════════════════════════════
# 2. compute_and_store_horizon persiste la clave
# ═══════════════════════════════════════════════════════════════

class TestComputeAndStoreHorizonPersistence:
    def test_persists_last_replan_ts_when_executes(self, db, monkeypatch):
        """compute_and_store_horizon persiste last_horizon_replan_ts (epoch)."""
        import time
        from api.services import planning_service as ps

        # Evitar el cooldown de 5 min: forzar global vacío (fixture) y no hidratar.
        # La clave en DB aún no existe → hidratación no aplica.
        before = time.time()
        result = ps.compute_and_store_horizon(horizon_days=7, db=db)
        assert result.get("total_slots", 0) >= 0  # ejecutó (sin skip de guards)

        raw = db.get_system_state("last_horizon_replan_ts")
        assert raw is not None, "compute_and_store_horizon debe persistir el timestamp"
        ts = float(raw)
        assert before - 5 <= ts <= time.time() + 5  # reciente

    def test_skipped_replan_does_not_reset_gate(self, db):
        """Un replan bloqueado (quiet-window activa) NO resetea el gate de 24h."""
        from datetime import datetime as dt
        from api.services import planning_service as ps

        # Activar ventana silenciosa: futuro lejano
        ps._post_full_replan_block_until = dt.now() + timedelta(minutes=60)
        db.set_system_state("last_horizon_replan_ts", str(1000.0))  # gate viejo

        result = ps.compute_and_store_horizon(horizon_days=7, db=db)
        assert result.get("skipped") is True
        # El gate NO debe haber cambiado (sigue en 1000.0)
        assert db.get_system_state("last_horizon_replan_ts") == "1000.0"


# ═══════════════════════════════════════════════════════════════
# 3. Marathon cooldown
# ═══════════════════════════════════════════════════════════════

class TestMarathonCooldown:
    def test_recent_marathon_skips_channel_and_picks_another(self, db):
        """Canal con marathon reciente → saltado; se elige otro elegible."""
        from api.services.marathon_service import select_marathon_channel

        # canal2 (id 3) maratoneó hace 1h → en cooldown (24h por defecto)
        db.record_marathon(3, "running")

        selected = select_marathon_channel(db)
        assert selected is not None
        assert selected[1] != 3, "el canal en cooldown no debe ser elegido"

    def test_all_channels_in_cooldown_returns_none(self, db):
        """Todos los canales en cooldown → select_marathon_channel devuelve None."""
        from api.services.marathon_service import select_marathon_channel

        for ch_id, _n, _s, _p, _a in CHANNELS:
            db.record_marathon(ch_id, "running")

        assert select_marathon_channel(db) is None

    def test_cooldown_expired_allows_channel_again(self, db):
        """Marathon de hace > MARATHON_COOLDOWN_HOURS → canal elegible de nuevo."""
        from api.services.marathon_service import select_marathon_channel

        # Aislar: solo el canal 3 (id 3) tiene marathon habilitado (los demás, no).
        # Fijamos MARATHON_COOLDOWN_HOURS=24 explícito para que el record de 25h
        # (> 24h) quede fuera de cooldown, sin depender del default del módulo.
        with db._connect() as conn:
            for ch_id, _n, _s, _p, _a in CHANNELS:
                cfg = {"videos_per_day": 1, "planning_enabled": True}
                cfg["MARATHON_ENABLED"] = (ch_id == 3)
                cfg["MARATHON_COOLDOWN_HOURS"] = 24
                conn.execute(
                    "UPDATE channels SET config_json = ? WHERE id = ?",
                    (json.dumps(cfg), ch_id),
                )
            conn.commit()

        # Forjar un record viejo (hace 25h > 24h de cooldown)
        old_date = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
        db.set_system_state(
            "last_marathon_3", json.dumps({"date": old_date, "status": "running"})
        )

        selected = select_marathon_channel(db)
        assert selected is not None
        assert selected[1] == 3
