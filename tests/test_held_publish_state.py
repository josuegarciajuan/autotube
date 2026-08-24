"""Tests del estado 'retenido' (hold a Privado por cuota agotada).

Cubre los cambios de ago 2026 que hacen que la UI y los loops NO traten a un
vídeo retenido como si fuera a publicarse:

  1. get_pipeline_status(): los items de "warming" llevan `held=true` cuando
     existe el marcador publish_hold_done_{yt_id}.
  2. _maybe_trigger_publish_verification(): omite vídeos retenidos (no genera
     alertas publish_not_detected falsas ni wall-scrapes).
  3. _channels_need_repack(): detecta canales con vídeos retenidos AUNQUE su
     target_public_at sea NULL (higiene de datos) — el repack los re-programa
     tras el reset de cuota.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture
def test_db_path(tmp_path, monkeypatch):
    """Temporary SQLite DB with the FULL real schema (init_db + migrate_v2)."""
    db_path = tmp_path / "test_held.db"

    from config import settings
    monkeypatch.setattr(settings, "DATABASE_PATH", str(db_path))

    from database.db import init_db
    from database.db_extended import migrate_v2
    init_db(str(db_path))
    migrate_v2(str(db_path))
    return db_path


@pytest.fixture
def db(test_db_path):
    """ExtendedDatabase pointing at the test DB (same trick as test_planning)."""
    from database.db_extended import ExtendedDatabase
    _db = ExtendedDatabase()
    _db._db_path = str(test_db_path)
    original_connect = _db._connect
    def _test_connect():
        conn = sqlite3.connect(str(test_db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn
    _db._connect = _test_connect
    return _db


def _seed(db, *, held: bool, target: str | None):
    """Channel canal2 + one uploaded_private video (+ optional hold marker)."""
    with db._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channels (id, name, slug, config_json) "
            "VALUES (3, 'Sincronias', 'canal2', '{}')"
        )
        conn.execute(
            """INSERT INTO videos (id, canal, channel_id, status, privacy_status,
                                  yt_video_id, titulo_final, target_public_at,
                                  uploaded_at, publish_mode, peak_source,
                                  video_path, published_retry_count)
               VALUES (100, 'canal2', 3, 'uploaded_private', 'private', 'yt_ABC123',
                       'Test video', ?, ?, 'scheduled', 'heuristic',
                       '/tmp/held.mp4', 0)""",
            (target, (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()),
        )
        if held:
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
                ("publish_hold_done_yt_ABC123", datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()


def test_pipeline_status_marks_held(db):
    _seed(db, held=True, target=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    status = db.get_pipeline_status()
    warming = status.get("warming", [])
    assert len(warming) == 1
    assert warming[0]["held"] is True


def test_pipeline_status_not_held_without_marker(db):
    _seed(db, held=False, target=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    status = db.get_pipeline_status()
    warming = status.get("warming", [])
    assert len(warming) == 1
    assert warming[0]["held"] is False


def test_publish_verify_skips_held(monkeypatch, db, test_db_path):
    """La verificación de publicación omite vídeos retenidos (no lanza thread)."""
    from database.db_extended import _maybe_trigger_publish_verification
    _seed(db, held=True, target=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    called = []
    monkeypatch.setattr(
        "database.db_extended._verify_published_status_bg",
        lambda *a, **k: called.append(a),
    )
    monkeypatch.setattr("database.db_extended.ExtendedDatabase", lambda: db)
    video = {
        "video_id": 100,
        "yt_video_id": "yt_ABC123",
        "channel_slug": "canal2",
        "target_public_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        "published_retry_at": None,
        "published_verified_at": None,
    }
    _maybe_trigger_publish_verification(video)
    assert called == []  # vídeo retenido → verificación omitida


def test_publish_verify_proceeds_when_not_held(monkeypatch, db, test_db_path):
    """Sin marcador de hold, un vídeo con target vencido SÍ lanza la verificación."""
    from database.db_extended import _maybe_trigger_publish_verification
    _seed(db, held=False, target=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    called = []
    monkeypatch.setattr(
        "database.db_extended._verify_published_status_bg",
        lambda *a, **k: called.append(a),
    )
    monkeypatch.setattr("database.db_extended.ExtendedDatabase", lambda: db)
    video = {
        "video_id": 100,
        "yt_video_id": "yt_ABC123",
        "channel_slug": "canal2",
        "target_public_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        "published_retry_at": None,
        "published_verified_at": None,
    }
    _maybe_trigger_publish_verification(video)
    assert called, "la verificación debe lanzarse para un vídeo no retenido"
    assert called[0] == (100, "canal2", "yt_ABC123")


def test_channels_need_repack_detects_held_with_null_target(monkeypatch, db):
    """Vídeo retenido con target NULL → el canal SÍ entra en el repack (síntoma 3)."""
    from api.services.upload_scheduler import _channels_need_repack
    _seed(db, held=True, target=None)  # higiene de datos: target NULL
    monkeypatch.setattr(
        "api.services.quota_tracker.get_channel_project",
        lambda slug: "youtube-uploads-automation",
    )
    affected = _channels_need_repack(db, datetime.now(timezone.utc))
    assert 3 in affected
