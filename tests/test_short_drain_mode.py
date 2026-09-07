"""Tests del MODO DRENAJE de shorts (short_drain_mode).

Verifica:
  1. El conteo de backlog es SOLO de nativos subibles (type='native',
     status='generated', archivo en disco, sin youtube_id) — los standalone
     no cuentan porque la válvula solo sube nativos.
  2. `_short_drain_enabled` refleja el flag persistido en system_state.
  3. `_short_drain_done` es True solo cuando TODOS los canales activos bajan
     del piso (SHORT_DRAIN_FLOOR_PER_CHANNEL).
  4. `_short_drain_auto_resume` apaga el flag solo cuando se alcanza el piso.
  5. `_fill_native_short_queue` NO rellena (devuelve None) con drenaje activo.
"""

import sqlite3

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services import shorts_scheduler as ss


_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
)
"""


def _mkdb(tmp_path, queued_by_channel: dict):
    """Build a temp DB with channels and queued native shorts rows.

    queued_by_channel: {channel_id: (slug, n_queued_native)} — se insertan
    `n_queued_native` shorts native 'generated' con archivo para ese canal.
    """
    path = tmp_path / "drain.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL DEFAULT '{}',
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shorts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                type TEXT NOT NULL DEFAULT 'native',
                title TEXT, status TEXT NOT NULL DEFAULT 'pending',
                file_path TEXT, published_at TIMESTAMP,
                youtube_id TEXT, youtube_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for cid, (slug, n) in queued_by_channel.items():
            conn.execute(
                "INSERT INTO channels (id, name, slug, config_json, active) "
                "VALUES (?, ?, ?, ?, 1)",
                (cid, slug, slug, "{}"),
            )
            for i in range(n):
                conn.execute(
                    "INSERT INTO shorts (channel_id, type, title, status, file_path) "
                    "VALUES (?, 'native', ?, 'generated', ?)",
                    (cid, f"short-{cid}-{i}", f"/tmp/drain-{cid}-{i}.mp4"),
                )
    return ExtendedDatabase(str(path))


@pytest.fixture(autouse=True)
def _point_db_path(tmp_path, monkeypatch):
    """Dirige config.settings.DATABASE_PATH a un temp DB por test.

    Los helpers de shorts_scheduler abren sqlite sobre
    `config.settings.DATABASE_PATH` (import en tiempo de llamada), así que basta
    con parchear ese atributo del módulo para aislarlos del DB de producción.
    """
    import config.settings as _settings
    monkeypatch.setattr(_settings, "DATABASE_PATH", str(tmp_path / "drain.db"))
    yield


def test_backlog_solo_nativos_subibles(tmp_path, _point_db_path):
    """El backlog ignora standalone y shorts sin archivo / ya subidos."""
    path = tmp_path / "drain.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL DEFAULT '{}',
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shorts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                type TEXT NOT NULL DEFAULT 'native',
                title TEXT, status TEXT NOT NULL DEFAULT 'pending',
                file_path TEXT, youtube_id TEXT
            );
        """)
        conn.execute(
            "INSERT INTO channels (id,name,slug,config_json,active) "
            "VALUES (1,'a','canalA','{}',1)"
        )
        # 2 nativos subibles
        for i in range(2):
            conn.execute(
                "INSERT INTO shorts (channel_id,type,status,file_path) "
                "VALUES (1,'native','generated','/tmp/x.mp4')"
            )
        # 1 standalone generated (no subible por válvula) -> no debe contar
        conn.execute(
            "INSERT INTO shorts (channel_id,type,status,file_path) "
            "VALUES (1,'standalone','generated','/tmp/s.mp4')"
        )
        # 1 native generado sin archivo -> no debe contar
        conn.execute(
            "INSERT INTO shorts (channel_id,type,status,file_path) "
            "VALUES (1,'native','generated','')"
        )
        # 1 native ya subido (youtube_id) -> no debe contar
        conn.execute(
            "INSERT INTO shorts (channel_id,type,status,file_path,youtube_id) "
            "VALUES (1,'native','published','/tmp/y.mp4','abc')"
        )
    assert ss._channel_queued_native_shorts(1) == 2
    assert ss.short_drain_backlog(db=ExtendedDatabase(str(path))) == {1: 2}


def test_done_requiere_todos_los_canales_bajo_piso(tmp_path, _point_db_path):
    _mkdb(tmp_path, {1: ("canal2", 50), 2: ("canal3", 2)})
    db = ExtendedDatabase(str(tmp_path / "drain.db"))
    # canal2=50 > piso => NO done
    assert ss._short_drain_done(db) is False
    # Bajar canal2 bajo piso
    with sqlite3.connect(str(tmp_path / "drain.db")) as conn:
        conn.execute(
            "UPDATE shorts SET status='published', youtube_id='x' WHERE id IN "
            "(SELECT id FROM shorts WHERE channel_id=1 ORDER BY id LIMIT 48)"
        )
    assert ss._channel_queued_native_shorts(1) <= 3
    assert ss._short_drain_done(db) is True


def test_auto_resume_solo_al_alcanzar_piso(tmp_path, _point_db_path):
    _mkdb(tmp_path, {1: ("canal2", 40)})
    db = ExtendedDatabase(str(tmp_path / "drain.db"))
    db.set_system_state("short_drain_mode", "true")
    assert ss._short_drain_enabled(db) is True
    # Backlog alto: no auto-resume aún
    assert ss._short_drain_auto_resume(db) is False
    assert ss._short_drain_enabled(db) is True
    # Vaciar cola bajo el piso -> auto-resume apaga el flag
    with sqlite3.connect(str(tmp_path / "drain.db")) as conn:
        conn.execute(
            "UPDATE shorts SET status='published', youtube_id='y' WHERE "
            "channel_id=1 AND status='generated'"
        )
    assert ss._short_drain_auto_resume(db) is True
    assert ss._short_drain_enabled(db) is False


def test_fill_bloqueado_con_drenaje_activo(tmp_path, _point_db_path):
    """Con drenaje activo, _fill_native_short_queue no rellena (None)."""
    _mkdb(tmp_path, {1: ("canal2", 40), 2: ("canal3", 40)})
    db = ExtendedDatabase(str(tmp_path / "drain.db"))
    db.set_system_state("short_drain_mode", "true")
    assert ss._short_drain_enabled(db) is True
    # El guard de drenaje corta antes de llegar a generar.
    assert ss._fill_native_short_queue(db=db, loop=None) is None
