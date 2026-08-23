"""Tests del quota_gate del repack (breaker real) y del filtro anti-thrash.

Cubre los fixes de ago 2026 que evitan que el repack martillee YouTube con
set_publish_at condenados (403) y que la DB se remueva sin converger:

  1. apply_publish_repack(quota_gate=True): si el breaker real
     `quota_exhausted_{project}` está activo → devuelve quota_skipped=1 SIN
     llamar a repack_channel_publish_times ni a set_publish_at.
  2. _channels_need_repack: excluye canales cuyo proyecto tiene el breaker
     activo (no entra en el repack hasta que la cuota se libere).
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from database.db import init_db
from api.services.upload_scheduler import _channels_need_repack


# ── Fake DB (suficiente para apply_publish_repack) ──────────────────

class FakeDB:
    def __init__(self, system_state=None, channels=None):
        self._state = dict(system_state or {})
        self._channels = channels or []

    def get_system_state(self, key):
        return self._state.get(key)

    def set_system_state(self, key, value):
        self._state[key] = value

    def is_channel_spam_blocked(self, channel_id):
        return False

    def get_channel(self, channel_id):
        for ch in self._channels:
            if ch["id"] == channel_id:
                return ch
        return None

    def get_channels(self, active_only=False):
        return self._channels


def test_apply_publish_repack_skips_when_project_breaker_active(monkeypatch):
    """Breaker `quota_exhausted_{project}` activo → quota_skipped, sin llamar al plan."""
    from api.services.publish_repack import apply_publish_repack

    db = FakeDB(
        system_state={"quota_exhausted_youtube-uploads-automation": "2026-08-23T21:07:33"},
        channels=[{
            "id": 3, "slug": "canal2",
            "config_json": '{"PUBLISH_TIMEZONE": "Europe/Madrid"}',
        }],
    )
    monkeypatch.setattr(
        "api.services.quota_tracker.get_channel_project",
        lambda slug: "youtube-uploads-automation",
    )

    # repack_channel_publish_times NO debe llamarse (el breaker corta antes)
    def _fail_repack(*a, **k):
        raise AssertionError("repack_channel_publish_times no debe llamarse con cuota agotada")
    monkeypatch.setattr(
        "pipeline.publish_scheduler.repack_channel_publish_times", _fail_repack,
    )

    res = apply_publish_repack(db, 3, "canal2", quota_gate=True)
    assert res["quota_skipped"] == 1
    assert res["total"] == 0
    assert res["rescheduled"] == 0


def test_apply_publish_repack_proceeds_without_breaker(monkeypatch):
    """Sin breaker activo y con capacidad → el repack calcula el plan (dry-run)."""
    from api.services.publish_repack import apply_publish_repack

    db = FakeDB(
        system_state={},  # sin breaker
        channels=[{
            "id": 3, "slug": "canal2",
            "config_json": '{"PUBLISH_TIMEZONE": "Europe/Madrid"}',
        }],
    )
    monkeypatch.setattr(
        "api.services.quota_tracker.get_channel_project",
        lambda slug: "youtube-uploads-automation",
    )
    monkeypatch.setattr(
        "api.services.quota_tracker.is_quota_exhausted_for_channel",
        lambda slug: False,
    )
    monkeypatch.setattr(
        "api.services.quota_tracker.project_has_free_capacity",
        lambda project, min_free_pct: True,
    )

    called = {}
    def _fake_repack(*a, **k):
        called["yes"] = True
        return []  # plan vacío → sin cambios
    monkeypatch.setattr(
        "pipeline.publish_scheduler.repack_channel_publish_times", _fake_repack,
    )

    res = apply_publish_repack(db, 3, "canal2", quota_gate=True)
    assert called.get("yes") is True
    assert res["quota_skipped"] == 0
    assert res["total"] == 0


# ── _channels_need_repack: filtro por proyecto en breaker ────────────

_VIDEOS_DDL = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER,
    status TEXT,
    publish_mode TEXT,
    target_public_at TEXT,
    uploaded_at TEXT,
    scheduled_upload_at TEXT,
    created_at TEXT
)
"""

_CHANNELS_DDL = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_SYSTEM_STATE_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
)
"""


def _db_with_videos(tmp_path, system_state=None):
    path = tmp_path / "rnc.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        # init_db crea una tabla videos antigua (canal, sin status/target):
        # la reemplazamos por el schema que usa el repack.
        conn.execute("DROP TABLE IF EXISTS videos")
        conn.executescript(_CHANNELS_DDL)
        conn.executescript(_VIDEOS_DDL)
        conn.executescript(_SYSTEM_STATE_DDL)
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json) "
            "VALUES (3, 'Sincronias', 'canal2', '{}')"
        )
        now = datetime.now(timezone.utc)
        # 2 vídeos del mismo canal con colisión < 24h → detectado como problemático
        conn.execute(
            "INSERT INTO videos (id, channel_id, status, publish_mode, target_public_at, created_at) "
            "VALUES (100, 3, 'uploaded_private', 'scheduled', ?, ?)",
            ((now + timedelta(hours=2)).isoformat(), now.isoformat()),
        )
        conn.execute(
            "INSERT INTO videos (id, channel_id, status, publish_mode, target_public_at, created_at) "
            "VALUES (101, 3, 'uploaded_private', 'scheduled', ?, ?)",
            ((now + timedelta(hours=4)).isoformat(), now.isoformat()),
        )
        for key, value in (system_state or {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
                (key, value),
            )
    from database.db_extended import ExtendedDatabase
    return ExtendedDatabase(str(path))


def test_channels_need_repack_excludes_breaker_blocked(monkeypatch, tmp_path):
    """Canal con proyecto en breaker → excluido del repack (anti-thrash)."""
    db = _db_with_videos(
        tmp_path,
        system_state={"quota_exhausted_youtube-uploads-automation": "2026-08-23T21:07:33"},
    )
    monkeypatch.setattr(
        "api.services.quota_tracker.get_channel_project",
        lambda slug: "youtube-uploads-automation",
    )
    affected = _channels_need_repack(db, datetime.now(timezone.utc))
    assert affected == []


def test_channels_need_repack_returns_channel_when_quota_free(monkeypatch, tmp_path):
    """Sin breaker → el canal con colisión SÍ entra en el repack."""
    db = _db_with_videos(tmp_path, system_state={})
    monkeypatch.setattr(
        "api.services.quota_tracker.get_channel_project",
        lambda slug: "youtube-uploads-automation",
    )
    affected = _channels_need_repack(db, datetime.now(timezone.utc))
    assert 3 in affected
