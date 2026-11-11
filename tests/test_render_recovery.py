"""Tests for the render/narration recovery loop."""
from __future__ import annotations

import json
import sqlite3

import pytest

from api.services import render_recovery as rr


class FakeDB:
    """Minimal DB facade over an in-memory SQLite connection."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY,
                canal TEXT,
                channel_id INTEGER,
                status TEXT,
                progress_phase TEXT,
                error_message TEXT,
                checkpoint_data TEXT,
                audio_path TEXT,
                yt_video_id TEXT,
                created_at TEXT
            );
            CREATE TABLE generation_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                video_id INTEGER,
                action TEXT,
                status TEXT,
                phase TEXT
            );
            CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
            """
        )

    def _connect(self):
        return self.conn

    def get_video(self, video_id):
        row = self.conn.execute(
            "SELECT * FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_system_state(self, key):
        row = self.conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_system_state(self, key, value):
        self.conn.execute(
            "INSERT INTO system_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def create_job(self, channel_id, action, video_id=None):
        cur = self.conn.execute(
            "INSERT INTO generation_jobs (channel_id, video_id, action, status) "
            "VALUES (?, ?, ?, 'queued')",
            (channel_id, video_id, action),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_video(self, video_id, checkpoint, *, status="error",
                  error="Body narration is longer than the video body by 2.2s",
                  yt_video_id=None, channel_id=3, canal="canal2"):
        self.conn.execute(
            "INSERT INTO videos (id, canal, channel_id, status, progress_phase, "
            "error_message, checkpoint_data, yt_video_id, created_at) "
            "VALUES (?, ?, ?, ?, 'video', ?, ?, ?, datetime('now'))",
            (video_id, canal, channel_id, status, error,
             json.dumps(checkpoint), yt_video_id),
        )
        self.conn.commit()


@pytest.fixture()
def media(tmp_path):
    audio = tmp_path / "narration.mp3"
    audio.write_bytes(b"fake")
    a1 = tmp_path / "scene_0.jpg"
    a2 = tmp_path / "scene_1.jpg"
    a1.write_bytes(b"img")
    a2.write_bytes(b"img")
    return {
        "audio": str(audio),
        "assets": [{"type": "image", "path": str(a1)},
                   {"type": "image", "path": str(a2)}],
    }


def _checkpoint(media):
    return {
        "tts": {"audio_path": media["audio"]},
        "media": {
            "assets": media["assets"],
            "scene_ranges": [{"duration": 5.0}, {"duration": 6.0}],
        },
    }


def test_queues_timeline_failure_with_intact_media(media):
    db = FakeDB()
    db.add_video(3001, _checkpoint(media))

    out = rr.recover_render_held_videos(db=db)

    assert out["queued"] == 1
    assert out["skipped"] == 0
    job = db.conn.execute("SELECT * FROM generation_jobs").fetchone()
    assert job["action"] == "reassemble"
    assert job["video_id"] == 3001
    assert job["status"] == "queued"
    assert db.get_system_state(rr._attempts_key(3001)) == "1"


def test_skips_purged_media(media):
    media["assets"][1]["path"] = str(media["assets"][1]["path"]) + ".missing"
    db = FakeDB()
    db.add_video(3001, _checkpoint(media))

    out = rr.recover_render_held_videos(db=db)

    assert out["queued"] == 0
    assert out["skipped"] == 1
    assert out["details"][0]["reason"].startswith("purged_assets_")
    assert db.conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0] == 0


def test_skips_uploaded_video(media):
    db = FakeDB()
    db.add_video(3001, _checkpoint(media), yt_video_id="abc123")

    out = rr.recover_render_held_videos(db=db)

    assert out["queued"] == 0
    assert out["scanned"] == 0


def test_skips_active_job(media):
    db = FakeDB()
    db.add_video(3001, _checkpoint(media))
    db.conn.execute(
        "INSERT INTO generation_jobs (channel_id, video_id, action, status) "
        "VALUES (3, 3001, 'reassemble', 'running')"
    )
    db.conn.commit()

    out = rr.recover_render_held_videos(db=db)

    assert out["queued"] == 0
    assert out["details"][0]["reason"] == "active_job"


def test_respects_attempt_budget(media):
    db = FakeDB()
    db.add_video(3001, _checkpoint(media))
    db.set_system_state(rr._attempts_key(3001), str(rr.MAX_ATTEMPTS))

    out = rr.recover_render_held_videos(db=db)

    assert out["queued"] == 0
    assert out["details"][0]["reason"] == "attempts_exhausted"


def test_dry_run_does_not_create_job(media):
    db = FakeDB()
    db.add_video(3001, _checkpoint(media))

    out = rr.recover_render_held_videos(db=db, dry_run=True)

    assert out["queued"] == 1
    assert out["details"][0]["action"] == "would_requeue"
    assert db.conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0] == 0
    assert db.get_system_state(rr._attempts_key(3001)) is None


def test_ignores_unrelated_error(media):
    db = FakeDB()
    db.add_video(3001, _checkpoint(media), error="Pollo AI quota exhausted")

    out = rr.recover_render_held_videos(db=db)

    assert out["queued"] == 0
    assert out["scanned"] == 0
