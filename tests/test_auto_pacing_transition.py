"""Tests de la transición automática de perfil (Fase 4 bis).

Verifica:
  1. clean_days_since_strike = inf sin señales de strike.
  2. strike → recovery tras N días limpios (umbral por defecto 7).
  3. Kill-switch (system_state auto_pacing_transition=false) desactiva.
  4. Un bloqueo activo resetea los días limpios a 0.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services import pacing_profile as pp

_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
)
"""

_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    channel_id INTEGER,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    title TEXT NOT NULL,
    message TEXT,
    acknowledged BOOLEAN DEFAULT 0,
    resolved BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _db(tmp_path):
    path = tmp_path / "apt.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.executescript(_ALERTS_DDL)
        # init_db no crea channels (lo hace migrate_v2): lo creamos con el
        # schema mínimo que ExtendedDatabase espera.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL DEFAULT '{}',
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # 1 canal para poder registrar señales de strike
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json) VALUES (1, 'Canal', 'canal2', '{}')"
        )
    return ExtendedDatabase(str(path))


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_clean_days_infinite_without_signals(tmp_path):
    db = _db(tmp_path)
    assert pp.clean_days_since_strike(db) == float("inf")


def test_active_block_resets_clean_days(tmp_path):
    db = _db(tmp_path)
    db.set_system_state("shorts_spam_blocked_until_1", "9999999999")  # futuro lejano
    # Con bloqueo activo los días limpios son ~0 (el epoch se mide en el mismo
    # instante; tolerancia de 1 segundo).
    assert pp.clean_days_since_strike(db) < 0.001


def test_removal_8_days_ago_triggers_strike_to_recovery(tmp_path):
    db = _db(tmp_path)
    # Remoción hace 8 días (> 7 del umbral por defecto)
    db.set_system_state(
        "shorts_spam_last_removal_1",
        json.dumps({"detected_at": _iso_days_ago(8), "strike_count": 1}),
    )
    result = pp.auto_transition_profile(db)
    assert result["transitioned"] is True
    assert result["from"] == "strike"
    assert result["to"] == "recovery"
    assert pp.get_active_profile_name(db) == "recovery"


def test_removal_recent_does_not_transition(tmp_path):
    db = _db(tmp_path)
    db.set_system_state(
        "shorts_spam_last_removal_1",
        json.dumps({"detected_at": _iso_days_ago(1), "strike_count": 1}),
    )
    result = pp.auto_transition_profile(db)
    assert result["transitioned"] is False
    assert pp.get_active_profile_name(db) == "strike"


def test_kill_switch_disables_transition(tmp_path):
    db = _db(tmp_path)
    db.set_system_state("auto_pacing_transition", "false")
    db.set_system_state(
        "shorts_spam_last_removal_1",
        json.dumps({"detected_at": _iso_days_ago(30), "strike_count": 1}),
    )
    result = pp.auto_transition_profile(db)
    assert result["transitioned"] is False
    assert result["reason"] == "kill-switch"


def test_recovery_to_normal_after_21_days(tmp_path):
    db = _db(tmp_path)
    # Transición a recovery primero
    db.set_system_state(
        "shorts_spam_last_removal_1",
        json.dumps({"detected_at": _iso_days_ago(25), "strike_count": 1}),
    )
    pp.set_pacing_profile("recovery", db)
    result = pp.auto_transition_profile(db)
    assert result["transitioned"] is True
    assert result["from"] == "recovery"
    assert result["to"] == "normal"
