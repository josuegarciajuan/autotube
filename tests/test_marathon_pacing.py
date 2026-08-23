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


def _db(tmp_path):
    path = tmp_path / "marathon.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
    return ExtendedDatabase(str(path))


def test_backlog_threshold_default_is_strike_4(tmp_path):
    db = _db(tmp_path)
    assert ms._marathon_backlog_per_channel(db) == 4


def test_backlog_threshold_follows_profile(tmp_path):
    from api.services import pacing_profile
    db = _db(tmp_path)
    pacing_profile.set_pacing_profile("normal", db)
    assert ms._marathon_backlog_per_channel(db) == 2
    pacing_profile.set_pacing_profile("recovery", db)
    assert ms._marathon_backlog_per_channel(db) == 3


def test_backlog_deep_with_no_channels_is_false(tmp_path):
    db = _db(tmp_path)
    assert ms.marathon_backlog_deep(db, active_channels=0) is False
