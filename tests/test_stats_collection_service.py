"""Tests del coordinador single-flight de stats (oct 2026).

Cubre el fix del falso "Timed out after 901s" (la recolección deep legítima
tarda 30-40 min y el antiguo watchdog de 900 s la marcaba error sin
cancelarla, liberando el guard para duplicados):

  - single-flight: dos acquire consecutivos → un solo run;
  - un run largo con heartbeat reciente sigue 'running' (sin watchdog);
  - 'stalled' es diagnóstico y NO libera el single-flight;
  - solo finish() cierra el run; el éxito SIEMPRE limpia 'error';
  - recover_on_startup: 'running' persistido → interrumpido por reinicio;
  - release_if_owner nunca suelta el run de otro.
"""
import sys
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from api.services import stats_collection_service as scs


class _TmpDB:
    """Stub mínimo de ExtendedDatabase para set/get_system_state."""

    def __init__(self, path):
        self._path = path

    def set_system_state(self, key, value):
        with sqlite3.connect(str(self._path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO system_state(key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def get_system_state(self, key):
        with sqlite3.connect(str(self._path)) as conn:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None


@pytest.fixture
def coord(tmp_path, monkeypatch):
    """Coordinador con system_state temporal (el pin deja de ser no-op)."""
    db_path = tmp_path / "state.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """CREATE TABLE system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now')))
            """
        )
    import database.db_extended as dbe
    monkeypatch.setattr(dbe, "ExtendedDatabase", lambda *a, **k: _TmpDB(db_path))
    return SimpleNamespace(c=scs.StatsCollectionCoordinator(), db_path=db_path)


def _read_state(db_path) -> dict:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = 'stats_collection_state'"
        ).fetchone()
    return json.loads(row[0]) if row else {}


# ── Single-flight ────────────────────────────────────────────

def test_acquire_y_single_flight(coord):
    ok, st = coord.c.try_acquire(deep=True)
    assert ok and st["status"] == "running" and st["run_id"]
    # Segunda petición NO adquiere: recibe el run activo para adjuntarse
    ok2, st2 = coord.c.try_acquire(deep=False)
    assert not ok2 and st2["run_id"] == st["run_id"]


def test_run_largo_con_heartbeat_sigue_running(coord):
    """El bug original: a los 901 s el watchdog marcaba error. Ahora un run
    de 40 min con progreso reciente sigue 'running' y el botón no se libera."""
    ok, _ = coord.c.try_acquire(deep=True)
    coord.c.heartbeat(phase="channel", channel_slug="canal2", total=4)
    coord.c._state["started_at"] = time.time() - 2400  # 40 min de run
    snap = coord.c.snapshot()
    assert snap["status"] == "running"
    assert snap["stalled"] is False
    assert coord.c.is_busy()


# ── stalled: diagnóstico, nunca terminal ─────────────────────

def test_stalled_es_diagnostico_y_no_libera(coord):
    ok, _ = coord.c.try_acquire()
    coord.c._state["last_progress_at"] = (
        time.time() - scs.STATS_STALL_THRESHOLD_S - 10
    )
    snap = coord.c.snapshot()
    assert snap["stalled"] is True
    assert snap["status"] == "running", "stalled NO debe cambiar el ciclo de vida"
    assert coord.c.is_busy(), "stalled NO debe liberar el single-flight"
    coord.c.finish(status="success", result="success")


# ── Ciclo de vida y error ────────────────────────────────────

def test_exito_limpia_error_previo(coord):
    """Bug oct 2026: el estado success arrastraba 'error: Timed out after 901s'."""
    ok, _ = coord.c.try_acquire()
    coord.c.finish(status="error", error="boom")
    ok2, _ = coord.c.try_acquire()
    assert ok2, "un run terminal libera el single-flight"
    snap = coord.c.snapshot()
    assert snap["status"] == "running" and snap["error"] is None, \
        "un run nuevo debe empezar limpio"
    coord.c.finish(status="success", result="success")
    snap = coord.c.snapshot()
    assert snap["status"] == "success" and snap["error"] is None


def test_error_terminal_persiste_en_db(coord):
    ok, _ = coord.c.try_acquire()
    coord.c.finish(status="error", error="token expirado")
    persisted = _read_state(coord.db_path)
    assert persisted["status"] == "error"
    assert persisted["error"] == "token expirado"


def test_record_channel_actualiza_progreso_y_persiste(coord):
    ok, _ = coord.c.try_acquire()
    coord.c.heartbeat(total=4)
    coord.c.record_channel({"slug": "canal2", "ok": True, "status": "collected"})
    snap = coord.c.snapshot()
    assert snap["completed_channels"] == 1 and snap["total_channels"] == 4
    assert len(snap["channels"]) == 1
    persisted = _read_state(coord.db_path)
    assert persisted["completed_channels"] == 1, "progreso persistido por canal"


# ── Recuperación en arranque ─────────────────────────────────

def test_recover_running_persistido_pasa_a_interrumpido(coord):
    with sqlite3.connect(str(coord.db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_state(key, value) VALUES (?, ?)",
            ("stats_collection_state", json.dumps({"status": "running", "run_id": "xyz"})),
        )
        conn.commit()
    c2 = scs.StatsCollectionCoordinator()
    c2.recover_on_startup()
    snap = c2.snapshot()
    assert snap["status"] == "error"
    assert "restart" in (snap["error"] or "").lower()
    assert not c2.is_busy(), "un run interrumpido NO puede bloquear el panel"


def test_recover_estado_terminal_se_conserva(coord):
    with sqlite3.connect(str(coord.db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_state(key, value) VALUES (?, ?)",
            ("stats_collection_state", json.dumps({"status": "success", "result": "success"})),
        )
        conn.commit()
    c2 = scs.StatsCollectionCoordinator()
    c2.recover_on_startup()
    snap = c2.snapshot()
    assert snap["status"] == "success" and snap["result"] == "success"


# ── release_if_owner: red de seguridad quirúrgica ────────────

def test_release_if_owner_no_suelta_run_ajeno(coord):
    ok, st = coord.c.try_acquire()
    assert ok
    coord.c.release_if_owner("run-inexistente")
    assert coord.c.is_busy(), "un run_id ajeno NO debe liberar el run activo"
    coord.c.finish(status="success", result="success")
    # tras terminal: no-op, sin desarmar futuros
    coord.c.release_if_owner(st["run_id"])
    assert not coord.c.is_busy()


# ── Integración: recolector + coordinador ────────────────────

def test_collector_deadline_y_progreso_propagados(tmp_path, monkeypatch):
    """`_collect_youtube_stats` pasa deadline de canal y progress al fetcher,
    cierra el run con estado limpio y marca over_channel_deadline."""
    import os
    os.environ["DATABASE_PATH"] = str(tmp_path / "wiring.db")
    sq = sqlite3.connect(str(tmp_path / "wiring.db"))
    sq.execute(
        """CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now')))
        """
    )
    sq.commit(); sq.close()

    import api.main as m
    from api.services import stats_collection_service as scs2

    m.TOKENS_DIR = tmp_path / "tokens"
    m.TOKENS_DIR.mkdir(exist_ok=True)
    (m.TOKENS_DIR / "canal2.pickle").write_bytes(b"stub")

    captured = {}

    class FakeFetcher:
        def __init__(self, slug):
            pass

        def collect_and_store(self, db, deep=False, use_data_api=True,
                              deadline=None, progress=None):
            captured["deadline"] = deadline
            captured["progress"] = progress
            return {"videos_updated": 5, "shorts_updated": 3,
                    "analytics_updated": 4, "channel_updated": True,
                    "authenticated": True, "analytics_available": True}

    class FakeDB:
        def get_channels(self, active_only=True):
            return [{"id": 1, "slug": "canal2", "name": "Test", "cpm_min": None}]

        def is_quota_exhausted_for_channel(self, slug):
            return False

        def record_stats_collection_run(self, *a, **k):
            pass

        def set_system_state(self, *a):
            pass

    import database.db_extended as dbe
    monkeypatch.setattr(dbe, "ExtendedDatabase", lambda *a, **k: FakeDB())
    import pipeline.youtube_stats as ys
    monkeypatch.setattr(ys, "YouTubeStatsFetcher", FakeFetcher)

    coord_api = m._stats_coordinator
    acquired, st = coord_api.try_acquire(deep=True)
    assert acquired
    m._collect_youtube_stats(deep=True, force=False, run_id=st["run_id"])

    assert not coord_api.is_busy(), "el run debe liberar al terminar"
    assert captured["deadline"] is not None, "debe pasar deadline de canal"
    assert captured["progress"] is not None, "debe pasar progress al fetcher"
    assert 0 < captured["deadline"] - st["started_at"] <= scs2.STATS_CHANNEL_MAX_DURATION_S + 1
    snap = coord_api.snapshot()
    assert snap["status"] == "success" and snap["error"] is None
    assert snap["channels"][0]["slug"] == "canal2"
    assert snap["channels"][0]["over_channel_deadline"] is False
    assert snap["completed_channels"] == 1 and snap["total_channels"] == 1


def test_collector_marca_over_channel_deadline(tmp_path, monkeypatch):
    """Un canal que agota su deadline queda marcado en el resumen y el run
    cierra limpio (success con resultado parcial de cobertura)."""
    import os
    os.environ["DATABASE_PATH"] = str(tmp_path / "wiring2.db")
    sq = sqlite3.connect(str(tmp_path / "wiring2.db"))
    sq.execute(
        """CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now')))
        """
    )
    sq.commit(); sq.close()

    import api.main as m

    m.TOKENS_DIR = tmp_path / "tokens2"
    m.TOKENS_DIR.mkdir(exist_ok=True)
    (m.TOKENS_DIR / "canal2.pickle").write_bytes(b"stub")

    class SlowFetcher:
        def __init__(self, slug):
            pass

        def collect_and_store(self, db, deep=False, use_data_api=True,
                              deadline=None, progress=None):
            return {"videos_updated": 1, "deadline_exceeded": True,
                    "authenticated": True, "analytics_available": True}

    class FakeDB:
        def get_channels(self, active_only=True):
            return [{"id": 1, "slug": "canal2", "name": "Test", "cpm_min": None}]

        def is_quota_exhausted_for_channel(self, slug):
            return False

        def record_stats_collection_run(self, *a, **k):
            pass

        def set_system_state(self, *a):
            pass

    import database.db_extended as dbe
    monkeypatch.setattr(dbe, "ExtendedDatabase", lambda *a, **k: FakeDB())
    import pipeline.youtube_stats as ys
    monkeypatch.setattr(ys, "YouTubeStatsFetcher", SlowFetcher)

    coord_api = m._stats_coordinator
    acquired, st = coord_api.try_acquire(deep=True)
    assert acquired
    m._collect_youtube_stats(deep=True, force=False, run_id=st["run_id"])
    snap = coord_api.snapshot()
    assert snap["status"] == "success" and snap["error"] is None
    assert snap["channels"][0]["over_channel_deadline"] is True
    assert not coord_api.is_busy()
