"""mark_video_uploaded must not fake the custom-thumbnail state.

Regression guard for the oct-2026 fix: the method no longer forces
``thumbnail_verified=1`` (which hid ``thumbnails.set()`` no-ops from the
verification service). It only writes the column when the caller passes the
real outcome.
"""

import sqlite3

from database.db_extended import ExtendedDatabase

_DDL = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL DEFAULT '{}',
    active BOOLEAN NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS youtube_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER, slug TEXT, name TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canal TEXT NOT NULL,
    channel_id INTEGER,
    video_path TEXT NOT NULL DEFAULT '',
    thumbnail_path TEXT,
    target_playlist_id INTEGER,
    status TEXT DEFAULT 'draft',
    yt_video_id TEXT, yt_url TEXT,
    uploaded_at TIMESTAMP,
    thumbnail_verified INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _db(tmp_path, verified: int):
    path = tmp_path / "mark.db"
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_DDL)
        conn.execute(
            "INSERT INTO videos (canal, video_path, thumbnail_verified) "
            "VALUES ('canal2', '/tmp/v.mp4', ?)",
            (verified,),
        )
        conn.commit()
    return ExtendedDatabase(str(path))


def test_mark_uploaded_preserves_verified_when_omitted(tmp_path):
    db = _db(tmp_path, verified=1)
    db.mark_video_uploaded(1, "abc123", "https://y/abc123", status="uploaded")
    v = db.get_video(1)
    assert v["yt_video_id"] == "abc123"
    assert v["thumbnail_verified"] == 1  # untouched


def test_mark_uploaded_sets_explicit_zero(tmp_path):
    db = _db(tmp_path, verified=1)
    db.mark_video_uploaded(1, "abc123", "https://y/abc123", status="uploaded",
                           thumbnail_verified=0)
    assert db.get_video(1)["thumbnail_verified"] == 0


def test_mark_uploaded_sets_explicit_one(tmp_path):
    db = _db(tmp_path, verified=0)
    db.mark_video_uploaded(1, "abc123", "https://y/abc123", status="uploaded",
                           thumbnail_verified=1)
    assert db.get_video(1)["thumbnail_verified"] == 1
