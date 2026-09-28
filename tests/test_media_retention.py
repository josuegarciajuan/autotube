"""Unit tests for pipeline.media_retention (retención 0 días).

Cubre:
  * normalización de basenames (str / dict-repr / PosixPath),
  * purga de un vídeo subido (borra mp4/audio/escenas; preserva thumb + SRT),
  * NO purga de vídeos/shorts sin subir,
  * preservación de assets compartidos con entidades pendientes,
  * respeto de media_file_locks,
  * idempotencia + sellado purged_at,
  * purga de shorts (native/clip),
  * clasificador de huérfanos (recientes/protegidos/bloqueados excluidos).
"""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import media_retention as mr  # noqa: E402


SCHEMA = """
CREATE TABLE videos (
    id INTEGER PRIMARY KEY, channel_id INTEGER, canal TEXT,
    video_path TEXT, thumbnail_path TEXT, audio_path TEXT,
    yt_video_id TEXT, status TEXT, checkpoint_data TEXT, media_purged_at TEXT
);
CREATE TABLE video_scenes (
    id INTEGER PRIMARY KEY, video_id INTEGER, image_path TEXT,
    audio_path TEXT, purged_at TEXT
);
CREATE TABLE video_asset_history (
    id INTEGER PRIMARY KEY, video_id INTEGER, file_path TEXT,
    asset_url TEXT, purged_at TEXT
);
CREATE TABLE shorts (
    id INTEGER PRIMARY KEY, channel_id INTEGER, type TEXT,
    file_path TEXT, thumbnail_path TEXT, youtube_id TEXT,
    status TEXT, media_purged_at TEXT
);
CREATE TABLE short_asset_history (
    id INTEGER PRIMARY KEY, short_id INTEGER, channel_id INTEGER,
    file_path TEXT, asset_url TEXT, purged_at TEXT
);
"""


class FakeDB:
    """Minimal ExtendedDatabase stand-in backed by a real temp SQLite file."""

    def __init__(self, path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.locked = set()
        self.error_paths = set()

    def _connect(self):
        return self._conn

    # ── helpers used by media_retention ──
    def get_video(self, video_id):
        row = self._connect().execute(
            "SELECT * FROM videos WHERE id=?", (video_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_scenes(self, video_id):
        return [dict(r) for r in self._connect().execute(
            "SELECT * FROM video_scenes WHERE video_id=?", (video_id,)
        ).fetchall()]

    def get_short(self, short_id):
        row = self._connect().execute(
            "SELECT * FROM shorts WHERE id=?", (short_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_locked_file_paths(self):
        return set(self.locked)

    def get_error_video_media_paths(self, max_age_hours=48):
        return set(self.error_paths)


def _write(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture()
def db(tmp_path):
    return FakeDB(tmp_path / "t.db")


# ── normalización ────────────────────────────────────────────────

def test_normalize_basename_variants():
    assert mr.normalize_basename("output/images/a.jpg") == "a.jpg"
    assert mr.normalize_basename("{'path': PosixPath('output/x/b.mp4'), 'source': 'p'}") == "b.mp4"
    assert mr.normalize_basename("{'path': 'output/y/c.jpg'}") == "c.jpg"
    assert mr.normalize_basename("") == ""
    assert mr.normalize_basename(None) == ""


# ── purga de vídeo ───────────────────────────────────────────────

def test_video_purge_deletes_material_preserves_thumb_and_srt(tmp_path, db):
    root = tmp_path
    mp4 = _write(root / "output" / "videos" / "v1.mp4")
    mp3 = _write(root / "output" / "audio" / "v1.mp3")
    srt = _write(root / "output" / "audio" / "v1_subtitles.srt")
    ts = _write(root / "output" / "audio" / "v1_timestamps.json")
    cta = _write(root / "output" / "audio" / "cta.mp3")
    cta_srt = _write(root / "output" / "audio" / "cta_subtitles.srt")
    img = _write(root / "output" / "images" / "s0.jpg")
    side = _write(root / "output" / "images" / "s0.pollo.json")
    thumb = _write(root / "output" / "thumbnails" / "v1.jpg")

    db._connect().executescript(f"""
        INSERT INTO videos (id, video_path, thumbnail_path, audio_path, yt_video_id,
                            status, checkpoint_data)
        VALUES (1, '{mp4}', '{thumb}', '{mp3}', 'YT1', 'published',
                '{{"tts": {{"cta_audio_path": "{cta}"}}}}');
        INSERT INTO video_scenes (video_id, image_path) VALUES (1, '{img}');
    """)
    db._connect().commit()

    report = mr.purge_entity_media(db, "video", 1)

    assert not mp4.exists()
    assert not mp3.exists()
    assert not cta.exists()
    assert not cta_srt.exists()
    assert not img.exists()
    assert not side.exists()
    # Preservados
    assert srt.exists()
    assert ts.exists()
    assert thumb.exists()
    assert report["freed_bytes"] > 0
    assert not report["errors"]

    row = db._connect().execute("SELECT * FROM videos WHERE id=1").fetchone()
    assert row["media_purged_at"]
    assert row["video_path"] == ""
    scene = db._connect().execute(
        "SELECT purged_at, image_path FROM video_scenes WHERE video_id=1"
    ).fetchone()
    assert scene["purged_at"] and scene["image_path"] == ""


def test_video_not_uploaded_is_never_purged(tmp_path, db):
    mp4 = _write(tmp_path / "output" / "videos" / "v2.mp4")
    db._connect().execute(
        "INSERT INTO videos (id, video_path, yt_video_id, status) VALUES (2, ?, NULL, 'awaiting_upload')",
        (str(mp4),),
    )
    db._connect().commit()

    report = mr.purge_entity_media(db, "video", 2)
    assert report["skipped"] == "not_uploaded"
    assert mp4.exists()


def test_shared_asset_referenced_by_pending_entity_is_preserved(tmp_path, db):
    shared = _write(tmp_path / "output" / "images" / "shared.jpg")
    db._connect().executescript(f"""
        INSERT INTO videos (id, yt_video_id, status) VALUES
            (10, 'YT10', 'published'), (11, NULL, 'awaiting_upload');
        INSERT INTO video_scenes (video_id, image_path) VALUES
            (10, '{shared}'), (11, '{shared}');
    """)
    db._connect().commit()

    report = mr.purge_entity_media(db, "video", 10)
    assert shared.exists(), "asset compartido con entidad pendiente no debe borrarse"
    assert str(shared) in report["skipped_protected"]


def test_locked_file_is_not_deleted(tmp_path, db):
    mp4 = _write(tmp_path / "output" / "videos" / "v3.mp4")
    db._connect().execute(
        "INSERT INTO videos (id, video_path, yt_video_id, status) VALUES (3, ?, 'YT3', 'published')",
        (str(mp4),),
    )
    db._connect().commit()
    db.locked = {str(mp4)}

    report = mr.purge_entity_media(db, "video", 3)
    assert mp4.exists()
    assert str(mp4) in report["skipped_locked"]


def test_purge_is_idempotent(tmp_path, db):
    mp4 = _write(tmp_path / "output" / "videos" / "v4.mp4")
    db._connect().execute(
        "INSERT INTO videos (id, video_path, yt_video_id, status) VALUES (4, ?, 'YT4', 'published')",
        (str(mp4),),
    )
    db._connect().commit()

    first = mr.purge_entity_media(db, "video", 4)
    second = mr.purge_entity_media(db, "video", 4)
    assert not mp4.exists()
    assert first["freed_bytes"] > 0
    assert second["freed_bytes"] == 0
    assert not second["errors"]


def test_asset_history_files_are_deleted(tmp_path, db):
    asset = _write(tmp_path / "output" / "images" / "hist.jpg")
    db._connect().executescript(f"""
        INSERT INTO videos (id, yt_video_id, status) VALUES (20, 'YT20', 'published');
        INSERT INTO video_asset_history (video_id, file_path) VALUES (20, '{asset}');
    """)
    db._connect().commit()

    mr.purge_entity_media(db, "video", 20)
    assert not asset.exists()


# ── purga de shorts ──────────────────────────────────────────────

def test_short_purge_deletes_file_and_assets_keeps_thumb(tmp_path, db):
    mp4 = _write(tmp_path / "output" / "videos" / "shorts" / "s1.mp4")
    asset = _write(tmp_path / "output" / "videos" / "shorts_clips" / "clip.mp4")
    thumb = _write(tmp_path / "output" / "thumbnails" / "s1.jpg")
    db._connect().executescript(f"""
        INSERT INTO shorts (id, file_path, thumbnail_path, youtube_id, status)
        VALUES (30, '{mp4}', '{thumb}', 'YTS30', 'published');
        INSERT INTO short_asset_history (short_id, file_path) VALUES (30, '{asset}');
    """)
    db._connect().commit()

    mr.purge_entity_media(db, "short", 30)
    assert not mp4.exists()
    assert not asset.exists()
    assert thumb.exists()
    row = db._connect().execute("SELECT * FROM shorts WHERE id=30").fetchone()
    assert row["media_purged_at"] and row["file_path"] == ""


def test_short_not_uploaded_is_never_purged(tmp_path, db):
    mp4 = _write(tmp_path / "output" / "videos" / "shorts" / "s2.mp4")
    db._connect().execute(
        "INSERT INTO shorts (id, file_path, youtube_id, status) VALUES (31, ?, NULL, 'generated')",
        (str(mp4),),
    )
    db._connect().commit()
    report = mr.purge_entity_media(db, "short", 31)
    assert report["skipped"] == "not_uploaded"
    assert mp4.exists()


# ── huérfanos ────────────────────────────────────────────────────

def test_reclaim_reason_excludes_protected_locked_recent(tmp_path):
    now = time.time()
    protected = {"shared.jpg"}
    locked = {"locked.mp4"}

    old = _write(tmp_path / "old.jpg")
    recent = _write(tmp_path / "recent.jpg")
    protected_file = _write(tmp_path / "shared.jpg")
    locked_file = _write(tmp_path / "locked.mp4")
    cut = now - 24 * 3600
    # recent mtime
    import os
    os.utime(recent, (now, now))
    os.utime(old, (now - 48 * 3600, now - 48 * 3600))
    os.utime(protected_file, (now - 48 * 3600, now - 48 * 3600))
    os.utime(locked_file, (now - 48 * 3600, now - 48 * 3600))

    assert mr._reclaim_reason(old, protected, locked, cut) is None
    assert mr._reclaim_reason(recent, protected, locked, cut) == "too_recent"
    assert mr._reclaim_reason(protected_file, protected, locked, cut) == "referenced_by_pending"
    assert mr._reclaim_reason(locked_file, protected, locked, cut) == "locked"


def test_purge_orphans_deletes_orphan_keeps_protected(tmp_path, db, monkeypatch):
    cat_dir = tmp_path / "pool"
    orphan = _write(cat_dir / "orphan.jpg")
    protected = _write(cat_dir / "shared.jpg")
    import os
    old = time.time() - 48 * 3600
    os.utime(orphan, (old, old))
    os.utime(protected, (old, old))

    monkeypatch.setitem(mr.SWEEP_CATEGORIES, "test_pool", (cat_dir, True))
    db._connect().execute(
        "INSERT INTO videos (id, yt_video_id, status) VALUES (40, NULL, 'awaiting_upload')"
    )
    db._connect().execute(
        "INSERT INTO video_scenes (video_id, image_path) VALUES (40, ?)", (str(protected),)
    )
    db._connect().commit()

    report = mr.purge_orphans(db, ["test_pool"], min_age_hours=24, dry_run=False)
    assert not orphan.exists()
    assert protected.exists()
    assert report["freed_bytes"] > 0


def test_classify_reclaim_does_not_delete(tmp_path, db, monkeypatch):
    cat_dir = tmp_path / "pool2"
    orphan = _write(cat_dir / "o.jpg")
    import os
    old = time.time() - 48 * 3600
    os.utime(orphan, (old, old))
    monkeypatch.setitem(mr.SWEEP_CATEGORIES, "test_pool2", (cat_dir, True))

    plan = mr.classify_reclaim(db, ["test_pool2"], min_age_hours=24)
    assert plan["reclaim_bytes"] > 0
    assert orphan.exists(), "classify_reclaim es dry-run puro"
