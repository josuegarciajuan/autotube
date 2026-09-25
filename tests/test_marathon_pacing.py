"""Tests del amplificador de maratones (Fase 3).

Verifica que el umbral de backlog para disparar maratones viene del perfil
central de pacing (strike=4, recovery=3, normal=2) y que el cálculo de
"backlog profundo" usa ese umbral.
"""

import sqlite3

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services import marathon_service as ms

_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
)
"""

_CHANNELS_DDL = """
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL DEFAULT '{}',
    active      BOOLEAN NOT NULL DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _db(tmp_path):
    path = tmp_path / "marathon.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.executescript(_CHANNELS_DDL)
        # init_db crea el esquema legacy de videos; añadimos las columnas que usa
        # el backlog (status/is_marathon) tal como hace migrate_v2 en producción.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE videos ADD COLUMN status TEXT")
        if "is_marathon" not in cols:
            conn.execute("ALTER TABLE videos ADD COLUMN is_marathon INTEGER DEFAULT 0")
        conn.commit()
    return ExtendedDatabase(str(path))


def test_backlog_threshold_default_is_strike_4(tmp_path):
    db = _db(tmp_path)
    assert ms._marathon_backlog_per_channel(db) == 4


def test_backlog_threshold_is_conservative_across_profiles(tmp_path):
    """Los maratones ya no se amplifican por perfil: umbral 4/con canal."""
    from api.services import pacing_profile
    db = _db(tmp_path)
    for profile in ("strike", "recovery", "normal"):
        pacing_profile.set_pacing_profile(profile, db)
        assert ms._marathon_backlog_per_channel(db) == 4, profile


def test_backlog_deep_with_no_channels_is_false(tmp_path):
    db = _db(tmp_path)
    assert ms.marathon_backlog_deep(db, active_channels=0) is False


def _seed_video(db, status, is_marathon=0):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO videos (canal, video_path, status, is_marathon) "
            "VALUES ('canal2', '', ?, ?)",
            (status, is_marathon),
        )
        conn.commit()


def test_backlog_excludes_marathons_and_warming(tmp_path):
    """El backlog solo cuenta no-maratones en awaiting_upload.

    Rompe el bucle maratón→backlog→maratón: un maratón en awaiting_upload (o en
    warming) no justifica generar el siguiente.
    """
    db = _db(tmp_path)
    _seed_video(db, "awaiting_upload", is_marathon=0)
    _seed_video(db, "awaiting_upload", is_marathon=1)   # maratón → no cuenta
    _seed_video(db, "uploaded_private", is_marathon=0)  # warming → no cuenta
    _seed_video(db, "uploaded_private", is_marathon=1)  # maratón warming → no cuenta

    assert ms.calculate_backlog(db) == 1


def test_backlog_deep_counts_only_eligible_channels(tmp_path):
    """El umbral usa canales con MARATHON_ENABLED=True, no todos los activos."""
    db = _db(tmp_path)
    db.create_channel("A", "canalA", {"MARATHON_ENABLED": True})
    db.create_channel("B", "canalB", {"MARATHON_ENABLED": False})

    # per_ch=4 × 1 canal elegible = 4. Con 3 no dispara, con 4 sí.
    for _ in range(3):
        _seed_video(db, "awaiting_upload", is_marathon=0)
    assert ms.marathon_backlog_deep(db) is False

    _seed_video(db, "awaiting_upload", is_marathon=0)
    assert ms.marathon_backlog_deep(db) is True


def test_channel_configs_enable_marathons_only_for_approved_channels(monkeypatch):
    from config import config_bridge

    monkeypatch.setattr(config_bridge, "_load_db_config", lambda slug: None)
    config_bridge._config_cache.clear()

    assert config_bridge.get_channel_config("canal2", force_reload=True).MARATHON_ENABLED is True
    assert config_bridge.get_channel_config("canal3", force_reload=True).MARATHON_ENABLED is False
    assert config_bridge.get_channel_config("canal4", force_reload=True).MARATHON_ENABLED is True
    assert config_bridge.get_channel_config("canal5", force_reload=True).MARATHON_ENABLED is True


def test_longform_cap_follows_profile_without_persisting_override(tmp_path):
    from api.services import pacing_profile

    db = _db(tmp_path)
    assert pacing_profile.get_pacing_value("max_longform_publish_day", db=db) == 1
    assert db.get_system_state("pacing_max_longform_publish_day") in (None, "")


def test_scheduled_marathon_gets_a_publish_target():
    target = ms._marathon_publish_target(
        "canal4",
        4,
        {"PUBLISH_MODE": "scheduled", "PUBLISH_TIMEZONE": "Europe/Madrid"},
        db=None,
    )
    assert target


def test_marathons_do_not_consume_normal_daily_publish_cap():
    from pipeline.publish_scheduler import _counts_toward_normal_daily_cap

    assert _counts_toward_normal_daily_cap({"is_marathon": 0}) is True
    assert _counts_toward_normal_daily_cap({"is_marathon": 1}) is False


def test_select_marathon_channel_excludes_disabled_channel(tmp_path):
    """The runtime gate reads MARATHON_ENABLED from the DB and must never
    select a channel whose flag is False (canal3's anti-spam block)."""
    import json
    db = _db(tmp_path)
    db.create_channel("A", "canalA", {"MARATHON_ENABLED": True})
    db.create_channel("B", "canalB", {"MARATHON_ENABLED": False})

    sel = ms.select_marathon_channel(db)
    # Only the enabled channel can be selected.
    assert sel is not None
    assert sel[0] == "canalA"


def test_select_marathon_channel_none_when_all_disabled(tmp_path):
    db = _db(tmp_path)
    db.create_channel("A", "canalA", {"MARATHON_ENABLED": False})
    db.create_channel("B", "canalB", {"MARATHON_ENABLED": False})
    assert ms.select_marathon_channel(db) is None


def test_min_interval_48h_blocks_recent_marathon_even_with_cooldown_zero(tmp_path):
    """Suelo duro de 48h: cooldown=0 NO permite repetir canal al instante."""
    db = _db(tmp_path)
    db.create_channel("A", "canalA", {"MARATHON_ENABLED": True, "MARATHON_COOLDOWN_HOURS": 0})
    db.record_marathon(1, "completed")  # recién maratoneado

    assert ms.select_marathon_channel(db) is None


def test_min_interval_expires_allows_channel(tmp_path):
    """Pasado el suelo de 48h el canal vuelve a ser elegible."""
    import json as _json
    from datetime import datetime as _dt, timedelta as _td
    db = _db(tmp_path)
    db.create_channel("A", "canalA", {"MARATHON_ENABLED": True, "MARATHON_COOLDOWN_HOURS": 0})
    old_date = (_dt.now() - _td(hours=49)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_system_state("last_marathon_1", _json.dumps({"date": old_date, "status": "completed"}))

    sel = ms.select_marathon_channel(db)
    assert sel is not None and sel[1] == 1
