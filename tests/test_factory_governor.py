"""Tests del gobernador de fábrica (Fase 4): disco + créditos LLM.

Verifica el fail-open (ante error de medición NO bloquea) y el bloqueo por
créditos LLM agotados.
"""

import sqlite3

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services import factory_governor as fg

_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
)
"""

_LLM_STATUS_DDL = """
CREATE TABLE IF NOT EXISTS llm_credit_status (
    provider     TEXT NOT NULL,
    status       TEXT NOT NULL,
    balance_usd  REAL,
    error_count_7d INTEGER DEFAULT 0,
    last_error   TEXT,
    metadata_json TEXT,
    checked_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _db(tmp_path):
    path = tmp_path / "factory.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.executescript(_LLM_STATUS_DDL)
    return ExtendedDatabase(str(path))


def test_credits_ok_when_no_status_recorded(tmp_path):
    # Fail-open: sin registro de créditos NO se bloquea
    db = _db(tmp_path)
    assert fg.credits_ok(db) is True


def test_credits_ok_with_healthy_deepseek(tmp_path):
    db = _db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO llm_credit_status (provider, status, balance_usd) VALUES ('deepseek', 'healthy', 10.0)"
        )
        conn.commit()
    assert fg.credits_ok(db) is True


def test_credits_blocked_when_deepseek_exhausted(tmp_path):
    db = _db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO llm_credit_status (provider, status, balance_usd) VALUES ('deepseek', 'exhausted', 0.0)"
        )
        conn.commit()
    assert fg.credits_ok(db) is False
    assert fg.factory_ok(db) is False


def test_disk_fail_open_on_measurement_error(monkeypatch):
    def _boom():
        raise RuntimeError("disk usage error")
    monkeypatch.setattr(fg, "free_disk_mb", _boom)
    assert fg.disk_ok() is True  # fail-open
