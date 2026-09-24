"""Tests del MODO DRENAJE de shorts (short_drain_mode).

Verifica:
  1. El conteo de backlog es SOLO de nativos subibles (type='native',
     status='generated', archivo en disco, sin youtube_id) — los standalone
     no cuentan porque la válvula solo sube nativos.
  2. `_short_drain_enabled` refleja el flag persistido en system_state.
  3. `_short_drain_done` es True solo cuando TODOS los canales activos bajan
     del piso (SHORT_DRAIN_FLOOR_PER_CHANNEL).
  4. `_short_drain_auto_resume` apaga el flag solo cuando se alcanza el piso.
  5. El drenaje es POR CANAL: solo suprime la generación de los canales con
     cola >= piso; los canales secos siguen generando (fix de inanición).
  6. `get_next_pending_shorts_slot(exclude_channel_ids=...)` respeta la
     exclusión de canales.
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
            CREATE TABLE IF NOT EXISTS shorts_planned_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                date_key TEXT NOT NULL,
                scheduled_at TIMESTAMP NOT NULL,
                target_upload_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'pending',
                short_type TEXT NOT NULL DEFAULT 'native',
                slot_position INTEGER DEFAULT 0,
                long_slot_position INTEGER,
                source_video_id INTEGER,
                short_id INTEGER,
                job_id INTEGER,
                retry_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


def test_fill_bloqueado_con_todos_los_canales_sobre_piso(tmp_path, _point_db_path):
    """Con drenaje activo y TODOS los canales sobre el piso, no se rellena."""
    _mkdb(tmp_path, {1: ("canal2", 40), 2: ("canal3", 40)})
    db = ExtendedDatabase(str(tmp_path / "drain.db"))
    db.set_system_state("short_drain_mode", "true")
    assert ss._short_drain_enabled(db) is True
    # Ambos canales sobre el piso => suprimidos => nada que rellenar.
    assert ss._drain_suppressed_channel_ids(db) == {1, 2}
    assert ss._fill_native_short_queue(db=db, loop=None) is None


def test_drenaje_suprime_solo_canales_sobre_piso(tmp_path, _point_db_path):
    """El drenaje POR CANAL solo suprime los canales con cola >= piso.

    Reproduce el bug de inanición: canal2 con backlog alto no debe impedir que
    canal3 (seco) siga generando.
    """
    _mkdb(tmp_path, {1: ("canal2", 44), 2: ("canal3", 0)})
    db = ExtendedDatabase(str(tmp_path / "drain.db"))
    # Drenaje apagado => sin supresión.
    assert ss._drain_suppressed_channel_ids(db) == set()
    # Drenaje activo => solo canal2 (44 >= 3) queda suprimido; canal3 (0) sigue libre.
    db.set_system_state("short_drain_mode", "true")
    suppressed = ss._drain_suppressed_channel_ids(db)
    assert suppressed == {1}
    assert 2 not in suppressed


def test_drenaje_se_apaga_cuando_todos_bajan_del_piso(tmp_path, _point_db_path):
    """Con un canal seco y otro sobre piso, el flag NO se apaga (drena el lleno)."""
    _mkdb(tmp_path, {1: ("canal2", 44), 2: ("canal3", 0)})
    db = ExtendedDatabase(str(tmp_path / "drain.db"))
    db.set_system_state("short_drain_mode", "true")
    assert ss._short_drain_auto_resume(db) is False
    assert ss._short_drain_enabled(db) is True


def test_get_next_pending_shorts_slot_excluye_canales(tmp_path, _point_db_path):
    """La consulta de slots respeta exclude_channel_ids (mecanismo del drenaje)."""
    _mkdb(tmp_path, {1: ("canal2", 0), 2: ("canal3", 0)})
    path = str(tmp_path / "drain.db")
    import datetime as _dt
    now = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(path) as conn:
        for cid in (1, 2):
            conn.execute(
                "INSERT INTO shorts_planned_slots "
                "(channel_id, date_key, scheduled_at, target_upload_at, status, short_type) "
                "VALUES (?, ?, ?, ?, 'pending', 'native')",
                (cid, now[:10], now, now),
            )
    db = ExtendedDatabase(path)
    # Sin exclusión: devuelve el primer slot pendiente (canal1, menor target).
    assert db.get_next_pending_shorts_slot()["channel_id"] == 1
    # Excluyendo canal1: salta al de canal2.
    assert db.get_next_pending_shorts_slot(exclude_channel_ids=[1])["channel_id"] == 2
    # Excluyendo ambos: no hay slot.
    assert db.get_next_pending_shorts_slot(exclude_channel_ids=[1, 2]) is None
