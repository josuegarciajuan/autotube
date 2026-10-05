"""Tests del janitor de jobs short huérfanos en 'retrying'."""
from __future__ import annotations

import os
import sqlite3

from database.db_extended import reap_stale_short_retrying, _pid_alive


def _mkconn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE generation_jobs (
            id INTEGER PRIMARY KEY, status TEXT, action TEXT, worker_pid INTEGER, channel_id INTEGER,
            started_at TIMESTAMP, created_at TIMESTAMP,
            error_msg TEXT, finished_at TIMESTAMP)"""
    )
    return c


def test_pid_alive():
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(999999) is False
    assert _pid_alive(None) is False


def test_reaps_stale_dead_worker_only():
    c = _mkconn()
    # 1: retrying, short, >6h, worker muerto → reclamar
    c.execute("INSERT INTO generation_jobs VALUES (1,'retrying','generate_native_short',999999,3,"
              "datetime('now','-10 hours'),datetime('now','-10 hours'),NULL,NULL)")
    # 2: retrying, short, >6h, worker VIVO → no tocar
    c.execute("INSERT INTO generation_jobs VALUES (2,'retrying','generate_native_short',?,3,"
              "datetime('now','-10 hours'),datetime('now','-10 hours'),NULL,NULL)", (os.getpid(),))
    # 3: retrying, short, reciente (<6h) → no tocar
    c.execute("INSERT INTO generation_jobs VALUES (3,'retrying','generate_native_short',NULL,3,"
              "datetime('now','-2 hours'),datetime('now','-2 hours'),NULL,NULL)")
    # 4: running → no es retrying
    c.execute("INSERT INTO generation_jobs VALUES (4,'running','generate_native_short',NULL,3,"
              "datetime('now','-10 hours'),datetime('now','-10 hours'),NULL,NULL)")
    # 5: retrying pero no es short → no tocar
    c.execute("INSERT INTO generation_jobs VALUES (5,'retrying','generate_only',NULL,3,"
              "datetime('now','-10 hours'),datetime('now','-10 hours'),NULL,NULL)")
    c.commit()

    reaped = reap_stale_short_retrying(c, timeout_minutes=360)
    assert [r["job_id"] for r in reaped] == [1]

    st = {row[0]: row[1] for row in c.execute("SELECT id, status FROM generation_jobs")}
    assert st[1] == "failed"
    assert st[2] == "retrying"
    assert st[3] == "retrying"
    assert st[4] == "running"
    assert st[5] == "retrying"
