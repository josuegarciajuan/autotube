"""Tests del ventaneado de errores OpenAI (auto-curación del estado).

Verifica que `check_openai_from_errors` solo cuenta errores dentro de
`OPENAI_QUOTA_ERROR_WINDOW_HOURS`, de modo que una caída antigua del fallback
OpenAI decae a `healthy` y no bloquea la generación indefinidamente.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services import llm_credit_checker as lcc

_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message     TEXT,
    title       TEXT,
    created_at  TIMESTAMP,
    resolved    BOOLEAN DEFAULT 0
)
"""

_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS script_generation_attempts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    error_message  TEXT,
    created_at     TIMESTAMP
)
"""


def _db(tmp_path):
    path = tmp_path / "credit.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_ALERTS_DDL)
        conn.executescript(_ATTEMPTS_DDL)
    return ExtendedDatabase(str(path))


def _stamp(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _insert_attempt(db, error_message: str, hours_ago: float):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO script_generation_attempts (error_message, created_at) VALUES (?, ?)",
            (error_message, _stamp(hours_ago)),
        )
        conn.commit()


def test_openai_healthy_when_only_old_errors_within_legacy_window(monkeypatch, tmp_path):
    # Errores de hace 2 días (fuera de la ventana de 24h) → NO cuentan → healthy.
    db = _db(tmp_path)
    monkeypatch.setattr(lcc, "OPENAI_QUOTA_ERROR_WINDOW_HOURS", 24)
    _insert_attempt(
        db,
        "Error code: 429 - You have no credits remaining",
        hours_ago=48,
    )
    res = lcc.check_openai_from_errors(db)
    assert res["status"] == "healthy"
    assert res["error_count_7d"] == 0


def test_openai_exhausted_when_recent_error_within_window(monkeypatch, tmp_path):
    # Error de cuota hace 1h (dentro de la ventana) → exhausted.
    db = _db(tmp_path)
    monkeypatch.setattr(lcc, "OPENAI_QUOTA_ERROR_WINDOW_HOURS", 24)
    _insert_attempt(
        db,
        "Error code: 429 - You have no credits remaining",
        hours_ago=1,
    )
    res = lcc.check_openai_from_errors(db)
    assert res["status"] == "exhausted"
    assert res["error_count_7d"] == 1


def test_openai_exhausted_only_if_error_within_window_even_with_old_ones(monkeypatch, tmp_path):
    # Mezcla: errores antiguos + uno reciente → exhausted (cuenta el reciente).
    db = _db(tmp_path)
    monkeypatch.setattr(lcc, "OPENAI_QUOTA_ERROR_WINDOW_HOURS", 24)
    _insert_attempt(db, "Error code: 429 - no credits", hours_ago=100)
    _insert_attempt(db, "Error code: 429 - no credits", hours_ago=2)
    res = lcc.check_openai_from_errors(db)
    assert res["status"] == "exhausted"
    assert res["error_count_7d"] == 1


def test_openai_exhausted_with_zero_window_keeps_legacy_7day_scan(monkeypatch, tmp_path):
    # Ventana 0 = mantener el scan histórico de 7 días (legacy): un error
    # reciente sigue contando → exhausted.
    db = _db(tmp_path)
    monkeypatch.setattr(lcc, "OPENAI_QUOTA_ERROR_WINDOW_HOURS", 0)
    _insert_attempt(db, "Error code: 429 - no credits", hours_ago=1)
    res = lcc.check_openai_from_errors(db)
    assert res["status"] == "exhausted"


def test_created_within_hours_boundary():
    assert lcc._created_within_hours(_stamp(0.5), 24) is True
    assert lcc._created_within_hours(_stamp(25), 24) is False
    assert lcc._created_within_hours("not-a-date", 24) is False
