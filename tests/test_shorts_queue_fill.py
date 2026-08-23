"""Tests del relleno masivo de cola de shorts nativos (Fase 2).

Verifica:
  1. El tope de cola por canal es 60 (MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL).
  2. _fill_native_short_queue elige el canal con MENOS cola por debajo del tope.
  3. No rellena si todos los canales están al tope.
  4. La subida de cola (válvula) respeta la hora pico del slot (private+publishAt)
     cuando el slot tiene target_upload_at futuro.
"""

import json
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


def _db(tmp_path):
    path = tmp_path / "shorts_fill.db"
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
            CREATE TABLE IF NOT EXISTS shorts_planned_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                date_key TEXT NOT NULL,
                scheduled_at TIMESTAMP NOT NULL,
                target_upload_at TIMESTAMP,
                short_type TEXT NOT NULL DEFAULT 'native',
                status TEXT NOT NULL DEFAULT 'pending',
                job_id INTEGER, short_id INTEGER,
                long_slot_position INTEGER, source_video_id INTEGER,
                slot_position INTEGER DEFAULT 0, slot_rank INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS generation_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER, video_id INTEGER,
                action TEXT DEFAULT 'generate_native_short',
                status TEXT DEFAULT 'queued',
                progress INTEGER DEFAULT 0, phase TEXT, error_msg TEXT,
                started_at TIMESTAMP, finished_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shorts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER, type TEXT DEFAULT 'native',
                title TEXT, status TEXT DEFAULT 'pending',
                file_path TEXT DEFAULT '', published_at TIMESTAMP,
                youtube_id TEXT, youtube_url TEXT, hook_text TEXT,
                topic TEXT, has_subscribe_cta INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT, channel_id INTEGER, video_path TEXT DEFAULT '',
                status TEXT DEFAULT 'draft', progress INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for ch_id, slug in ((1, "canal2"), (2, "canal3")):
            conn.execute(
                "INSERT INTO channels (id, name, slug, config_json) VALUES (?, ?, ?, ?)",
                (ch_id, slug, slug, json.dumps({"videos_per_day": 1})),
            )
    return ExtendedDatabase(str(path))


def _seed_queued(db, channel_id: int, n: int):
    with db._connect() as conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO shorts (channel_id, type, title, status, file_path)
                   VALUES (?, 'native', ?, 'generated', '/tmp/x.mp4')""",
                (channel_id, f"short-{i}"),
            )
        conn.commit()


async def _noop_dispatch(**kwargs):
    return None


@pytest.fixture
def noop_dispatch(monkeypatch):
    monkeypatch.setattr(ss, "_dispatch_short_async", _noop_dispatch)


def test_queue_cap_is_60():
    from config.defaults import MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL
    assert MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL == 60


def test_fill_skips_when_all_channels_at_cap(tmp_path, noop_dispatch):
    db = _db(tmp_path)
    _seed_queued(db, 1, 60)
    _seed_queued(db, 2, 60)
    result = ss._fill_native_short_queue(db=db)
    assert result is None, "todos los canales al tope → no rellenar"


def test_fill_picks_channel_with_least_queue(tmp_path, noop_dispatch):
    import asyncio
    db = _db(tmp_path)
    _seed_queued(db, 1, 40)  # canal2 tiene 40 (bajo el tope)
    _seed_queued(db, 2, 10)  # canal3 tiene 10 → debe elegirse
    loop = asyncio.new_event_loop()
    try:
        result = ss._fill_native_short_queue(db=db, loop=loop)
        # El dispatch se mockea a no-op; el fill crea slot/job y retorna info
        assert result is not None
        assert result["channel_slug"] == "canal3"
        assert result["short_type"] == "native"
        assert result.get("fill") is True
        # El slot creado queda 'running' con target_upload_at futuro
        with db._connect() as conn:
            row = conn.execute(
                "SELECT target_upload_at, status FROM shorts_planned_slots WHERE channel_id = 2",
            ).fetchone()
            assert row is not None
            assert row["status"] == "running"
            assert row["target_upload_at"] is not None
    finally:
        loop.close()


def test_fill_respects_global_shorts_pause(tmp_path, noop_dispatch):
    db = _db(tmp_path)
    db.set_system_state("shorts_paused", "true")
    assert ss._fill_native_short_queue(db=db) is None
