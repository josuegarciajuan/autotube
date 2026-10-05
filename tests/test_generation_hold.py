"""Tests de F0 — pausa de generación (hold) a través de reinicios.

Contrato: la pausa sostiene dos centinelas en generation_jobs (phase='hold');
la recuperación de arranque y la reconexión NO deben tumbaros, y la intención
del operador debe restaurarlos.
"""
import contextlib
import sqlite3

import pytest

from api.services import generation_hold as gh


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
"""


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO channels(id, slug) VALUES (6, 'test')")
    conn.execute("INSERT INTO channels(id, slug) VALUES (3, 'canal2')")
    conn.commit()
    conn.close()
    return FakeDB(path)


def _rows(db, sql, params=()):
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def test_ensure_hold_creates_running_sentinels(db):
    res = gh.ensure_hold(db, reason="test", ttl_days=2)
    assert res["created"] == 2
    rows = _rows(db, "SELECT action, status, phase FROM generation_jobs")
    assert {r["action"] for r in rows} == {"generate_only", "generate_native_short"}
    assert all(r["status"] == "running" and r["phase"] == "hold" for r in rows)
    assert gh.is_generation_hold_active(db) is True


def test_ensure_hold_creates_n_longform_sentinels_with_dist_render(db, monkeypatch):
    """Con render distribuido (límite 2) la pausa necesita 2 centinelas long-form."""
    monkeypatch.setenv("AUTOTUBE_DIST_RENDER_V2", "1")
    res = gh.ensure_hold(db, ttl_days=2)
    assert res["created"] == 3
    actions = [r["action"] for r in _rows(db, "SELECT action FROM generation_jobs WHERE phase='hold'")]
    assert actions.count("generate_only") == 2
    assert actions.count("generate_native_short") == 1


def test_ensure_hold_shrinks_when_limit_drops(db, monkeypatch):
    """Si el límite baja de 2 a 1, el centinela long-form sobrante se cancela."""
    monkeypatch.setenv("AUTOTUBE_DIST_RENDER_V2", "1")
    gh.ensure_hold(db)
    monkeypatch.delenv("AUTOTUBE_DIST_RENDER_V2", raising=False)
    gh.ensure_hold(db)
    rows = _rows(db, "SELECT action, status FROM generation_jobs WHERE phase='hold'")
    running = [r for r in rows if r["status"] == "running"]
    assert len(running) == 2
    assert sum(1 for r in running if r["action"] == "generate_only") == 1


def test_ensure_hold_is_idempotent(db):
    gh.ensure_hold(db)
    gh.ensure_hold(db)
    n = _rows(db, "SELECT COUNT(*) c FROM generation_jobs WHERE phase='hold'")[0]["c"]
    assert n == 2


def test_refresh_hold_updates_heartbeat(db):
    gh.ensure_hold(db)
    with db._connect() as conn:
        conn.execute(
            "UPDATE generation_jobs SET last_heartbeat_at='2000-01-01 00:00:00' WHERE phase='hold'"
        )
    assert gh.refresh_hold(db) == 2
    rows = _rows(db, "SELECT last_heartbeat_at FROM generation_jobs WHERE phase='hold'")
    assert all(str(r["last_heartbeat_at"]) > "2020" for r in rows)


def test_release_hold_requires_confirmation(db):
    gh.ensure_hold(db)
    with pytest.raises(ValueError):
        gh.release_hold(db, confirm="nope")
    res = gh.release_hold(db, confirm=gh.RELEASE_CONFIRM)
    assert res["released"] == 2
    assert gh.is_generation_hold_active(db) is False


def test_reconcile_without_intent_does_not_invent_pause(db):
    """Sin intención previa no se crea una pausa (no se inventa el hold)."""
    res = gh.reconcile_on_startup(db)
    assert res["intent"] is False
    assert res["ensured"] is None
    assert gh.is_generation_hold_active(db) is False
    assert _rows(db, "SELECT COUNT(*) c FROM generation_jobs")[0]["c"] == 0


def test_reconcile_restores_hold_after_restart_killed_sentinels(db):
    """Reproduce el bug real: el arranque marca los centinelas como failed."""
    gh.set_hold_intent(db, True, reason="mantener parada la generación")
    gh.ensure_hold(db)
    # Simula lo que hacía auto_recover_on_startup / reconnect:
    with db._connect() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status='failed', "
            "error_msg='Server restarted — old process no longer exists' WHERE phase='hold'"
        )
    gh.set_hold_intent(db, True, reason="mantener parada la generación")
    res = gh.reconcile_on_startup(db)
    assert res["intent"] is True
    assert res["active"] is True
    rows = _rows(db, "SELECT status, phase FROM generation_jobs WHERE phase='hold'")
    assert len(rows) == 2 and all(r["status"] == "running" for r in rows)


def test_active_hold_rows_excludes_expired_heartbeat(db):
    gh.set_hold_intent(db, False)
    gh.ensure_hold(db)
    with db._connect() as conn:
        conn.execute(
            "UPDATE generation_jobs SET last_heartbeat_at='2000-01-01 00:00:00' WHERE phase='hold'"
        )
    assert gh.active_hold_rows(db) == []
    # Sin intención y sin centinelas vigentes, el hold no está activo.
    assert gh.is_generation_hold_active(db) is False


def test_reconcile_adopts_existing_sentinels(db):
    """Sin intención previa, adopta centinelas vigentes (no inventa, adopta)."""
    gh.ensure_hold(db)  # centinelas existen; no hay intent registrado
    with db._connect() as conn:
        conn.execute("DELETE FROM system_state WHERE key = ?", (gh.INTENT_KEY,))
    res = gh.reconcile_on_startup(db)
    assert res["intent"] is True
    assert gh.hold_intent(db) is True
    assert gh.is_generation_hold_active(db) is True


def test_released_hold_is_not_readopted(db):
    """Un hold liberado explícitamente no debe re-activarse solo."""
    gh.set_hold_intent(db, True)
    gh.ensure_hold(db)
    gh.release_hold(db, confirm=gh.RELEASE_CONFIRM)
    res = gh.reconcile_on_startup(db)
    assert res["intent"] is False
    assert gh.is_generation_hold_active(db) is False


def test_is_hold_row():
    assert gh.is_hold_row({"phase": "hold"}) is True
    assert gh.is_hold_row({"phase": "video"}) is False
    assert gh.is_hold_row({}) is False
