"""Tests de frescura en publicación (Fase 4 bis).

Verifica:
  1. is_stale: vídeo viejo (> umbral) es stale; reciente no.
  2. refresh_stale_video: sin guion → fail-open (no lanza, refreshed False).
  3. refresh_stale_video con LLM mockeado → persiste título nuevo.
"""

import json
import sqlite3

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase
from api.services import freshness as fr

_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now'))
)
"""


def _db(tmp_path):
    path = tmp_path / "freshness.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL DEFAULT '{}',
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json) VALUES (1, 'Canal', 'canal2', '{}')"
        )
        # La tabla base de init_db no tiene columnas v2 (status, title_options,
        # channel_id...): la recreamos con el schema que espera el código.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS youtube_playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER, slug TEXT, name TEXT,
                description TEXT, playlist_type TEXT,
                youtube_playlist_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            DROP TABLE IF EXISTS videos;
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                script_id INTEGER, canal TEXT NOT NULL,
                channel_id INTEGER,
                video_path TEXT NOT NULL DEFAULT '',
                thumbnail_path TEXT, audio_path TEXT,
                status TEXT DEFAULT 'draft',
                titulo_final TEXT, title_options TEXT, description TEXT,
                tags_json TEXT,
                yt_video_id TEXT, yt_url TEXT,
                target_public_at TIMESTAMP, scheduled_upload_at TIMESTAMP,
                target_playlist_id INTEGER,
                uploaded_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    return ExtendedDatabase(str(path))


def _seed_video(db, age_days: int, with_script: bool = True) -> int:
    with db._connect() as conn:
        if with_script:
            conn.execute(
                """INSERT INTO scripts (canal, guion, keywords_json, titulo_options, escenas_json)
                   VALUES ('canal2', 'Guion de prueba con historia impactante.', '["historia"]', '[]', '[]')"""
            )
            script_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        else:
            script_id = None
        conn.execute(
            """INSERT INTO videos (canal, script_id, video_path, status, titulo_final,
                                   created_at)
               VALUES ('canal2', ?, '/tmp/v.mp4', 'awaiting_upload', 'Título viejo',
                       datetime('now', 'localtime', ?))""",
            (script_id, f"-{age_days} days"),
        )
        vid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.commit()
        return vid


def test_is_stale_threshold(tmp_path):
    db = _db(tmp_path)
    old = _seed_video(db, age_days=10)
    recent = _seed_video(db, age_days=1)
    assert fr.is_stale(db, old) is True
    assert fr.is_stale(db, recent) is False


def test_refresh_fail_open_without_script(tmp_path):
    db = _db(tmp_path)
    vid = _seed_video(db, age_days=10, with_script=False)
    result = fr.refresh_stale_video(db, vid)
    assert result["refreshed"] is False
    assert result["reason"] == "no guion"


def test_refresh_persists_new_title(tmp_path, monkeypatch):
    db = _db(tmp_path)
    vid = _seed_video(db, age_days=10)

    class FakeMeta:
        def __init__(self, cfg):
            pass

        def generate(self, script, source_content=None):
            return {"titles": ["Título viral nuevo"], "selected_title": "Título viral nuevo"}

    async def fake_thumb(video_id):
        return "/tmp/thumb_new.jpg"

    monkeypatch.setattr("pipeline.metadata_generator.MetadataGenerator", FakeMeta)
    monkeypatch.setattr("api.services.thumbnail_service.regenerate_thumbnail_for_video", fake_thumb)

    result = fr.refresh_stale_video(db, vid)
    assert result["refreshed"] is True
    assert result["new_title"] == "Título viral nuevo"
    v = db.get_video(vid)
    assert v["titulo_final"] == "Título viral nuevo"
    titles = json.loads(v["title_options"])
    assert "Título viral nuevo" in titles


def test_refresh_recent_video_noop(tmp_path, monkeypatch):
    db = _db(tmp_path)
    vid = _seed_video(db, age_days=1)

    class FakeMeta:
        def generate(self, script, source_content=None):
            return {"selected_title": "NO DEBE USARSE"}

    monkeypatch.setattr("pipeline.metadata_generator.MetadataGenerator", FakeMeta)
    result = fr.refresh_stale_video(db, vid)
    assert result["refreshed"] is False
    assert result["reason"] == "not stale"
