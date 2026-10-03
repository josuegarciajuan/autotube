"""Tests de la exposición HTTP de la pausa de generación (panel Dashboard).

Contrato: el toggle ``/api/system/generation-pause`` pausa SOLO la creación
(long-form + shorts) mediante los centinelas de ``generation_hold``; las
subidas permanecen intactas. Al reanudar debe salir del modo drenaje de shorts.
"""
import contextlib
import sqlite3

import pytest

from api.services import generation_hold as gh
from api.routers import system as system_router


class FakeDB:
    def __init__(self, path):
        self.path = str(path)

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_system_state(self, key):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_system_state(self, key, value):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO system_state(key, value, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=datetime('now')",
                (key, value),
            )


SCHEMA = """
CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT NOT NULL, active INTEGER DEFAULT 1);
CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE generation_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT,
    progress INTEGER DEFAULT 0,
    error_msg TEXT,
    started_at TEXT,
    finished_at TEXT,
    last_heartbeat_at TEXT,
    worker_pid INTEGER,
    video_id INTEGER
);
CREATE TABLE pipeline_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT,
    resolved INTEGER DEFAULT 0,
    resolved_at TEXT
);
"""


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO channels(id, slug) VALUES (6, 'test')")
    conn.commit()
    conn.close()
    fake = FakeDB(path)
    monkeypatch.setattr(system_router, "get_db", lambda: fake)
    return fake


def _rows(db, sql, params=()):
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _update(enabled, reason=None):
    return system_router._GenerationPauseUpdate(enabled=enabled, reason=reason)


def test_status_default_not_paused(db):
    st = system_router.get_generation_pause()
    assert st["ok"] is True
    assert st["active"] is False
    assert st["intent"] is False
    assert st["blocks"] == {"longform": True, "shorts": True}


def test_enable_creates_sentinels_and_blocks_generation(db):
    st = system_router.set_generation_pause(_update(True, reason="test panel"))
    assert st["active"] is True
    assert st["intent"] is True
    rows = _rows(db, "SELECT action, status, phase FROM generation_jobs WHERE phase='hold'")
    assert {r["action"] for r in rows} == {"generate_only", "generate_native_short"}
    assert all(r["status"] == "running" for r in rows)
    # El estado se publica para el panel.
    assert db.get_system_state(gh.STATE_KEY)


def test_disable_releases_hold_and_clears_drain(db):
    db.set_system_state("short_drain_mode", "true")
    system_router.set_generation_pause(_update(True))
    st = system_router.set_generation_pause(_update(False))
    assert st["active"] is False
    assert st["intent"] is False
    assert db.get_system_state("short_drain_mode") == "false"
    rows = _rows(db, "SELECT status FROM generation_jobs WHERE phase='hold'")
    assert rows and all(r["status"] == "cancelled" for r in rows)


def test_uploads_are_not_touched_by_hold(db):
    """Un job de subida preexistente debe seguir intacto al pausar generación."""
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO generation_jobs(channel_id, action, status, phase) "
            "VALUES (6, 'upload_only', 'running', 'upload')"
        )
    system_router.set_generation_pause(_update(True))
    up = _rows(db, "SELECT status, phase FROM generation_jobs WHERE action='upload_only'")
    assert len(up) == 1 and up[0]["status"] == "running" and up[0]["phase"] == "upload"
    # Los centinelas no son jobs de subida.
    assert _rows(db, "SELECT COUNT(*) c FROM generation_jobs WHERE phase='hold' AND action='upload_only'")[0]["c"] == 0
