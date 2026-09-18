"""Tests del contador de racha de fallos (circuit breaker de canal).

Regresión: el contador sólo miraba jobs `failed`, así que un éxito intercalado
no reseteaba la racha y el aviso `consecutive_failures` se recreaba al resolverlo.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.services.planning_service import (  # noqa: E402
    count_channel_consecutive_failures,
)

_PERMANENT = "Guion repetido (sim=1.00 vs script #1320)"
_TRANSIENT = "Server restarted — old process no longer exists"


class FakeDB:
    def __init__(self, jobs: list[tuple[str, str]]):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE generation_jobs (
                   id INTEGER PRIMARY KEY,
                   channel_id INTEGER,
                   status TEXT,
                   error_msg TEXT,
                   action TEXT,
                   created_at TEXT
               )"""
        )
        for idx, (status, err) in enumerate(jobs, start=1):
            self.conn.execute(
                """INSERT INTO generation_jobs
                   (id, channel_id, status, error_msg, action, created_at)
                   VALUES (?, 3, ?, ?, 'generate_only', '2099-01-01 00:00:00')""",
                (idx, status, err),
            )
        self.conn.commit()

    @contextlib.contextmanager
    def _connect(self):
        yield self.conn


def test_counts_consecutive_permanent_failures():
    db = FakeDB([
        ("failed", _PERMANENT),
        ("failed", _PERMANENT),
        ("failed", _PERMANENT),
    ])
    assert count_channel_consecutive_failures(db, 3) == 3


def test_success_resets_streak():
    # oldest → newest: 3 fallos, luego un éxito (id mayor) = 0 fallos vigentes.
    db = FakeDB([
        ("failed", _PERMANENT),
        ("failed", _PERMANENT),
        ("failed", _PERMANENT),
        ("completed", ""),
    ])
    assert count_channel_consecutive_failures(db, 3) == 0


def test_transient_failure_resets_streak():
    db = FakeDB([
        ("failed", _PERMANENT),
        ("failed", _TRANSIENT),
    ])
    assert count_channel_consecutive_failures(db, 3) == 0
