"""Unit tests for pipeline.media_retention (retención 0 días).

Cubre:
  * normalización de basenames (str / dict-repr / PosixPath),
  * purga de un vídeo subido (borra mp4/audio/escenas; preserva thumb + SRT),
  * NO purga de vídeos/shorts sin subir,
  * preservación de assets compartidos con entidades pendientes,
  * respeto de media_file_locks,
  * idempotencia + sellado purged_at,
  * purga de shorts (native/clip),
  * clasificador de huérfanos (recientes/protegidos/bloqueados excluidos),
  * barrido de segment-_dirs (oct 2026): uploaded/pending/orphans/locks,
    purga de segments en la fuente post-subida, idempotencia y manifiesto,
  * stale_pending_entities (report-only).
"""

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import media_retention as mr  # noqa: E402


SCHEMA = """
CREATE TABLE videos (
    id INTEGER PRIMARY KEY, channel_id INTEGER, canal TEXT,
    video_path TEXT, thumbnail_path TEXT, audio_path TEXT,
    yt_video_id TEXT, status TEXT, checkpoint_data TEXT, media_purged_at TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE video_scenes (
    id INTEGER PRIMARY KEY, video_id INTEGER, image_path TEXT,
    audio_path TEXT, purged_at TEXT
);
CREATE TABLE video_asset_history (
    id INTEGER PRIMARY KEY, video_id INTEGER, file_path TEXT UNIQUE,
    asset_url TEXT, purged_at TEXT
);
CREATE TABLE shorts (
    id INTEGER PRIMARY KEY, channel_id INTEGER, type TEXT,
    file_path TEXT, thumbnail_path TEXT, youtube_id TEXT,
    status TEXT, media_purged_at TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE short_asset_history (
    id INTEGER PRIMARY KEY, short_id INTEGER, channel_id INTEGER,
    file_path TEXT UNIQUE, asset_url TEXT, purged_at TEXT
);
CREATE TABLE generation_jobs (
    id INTEGER PRIMARY KEY, video_id INTEGER, status TEXT, action TEXT,
    phase TEXT, result_short_id INTEGER
);
CREATE TABLE media_file_locks (
    id INTEGER PRIMARY KEY, job_id INTEGER, file_path TEXT, locked_at TEXT
);
"""


def _ts(*, hours_ago=0, days_ago=0):
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago, days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


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

    # ── test-side helpers ──
    def add_video(self, **kw):
        cols = {"id": None, "video_path": None, "thumbnail_path": None,
                "audio_path": None, "yt_video_id": None, "status": None,
                "checkpoint_data": None, "created_at": _ts(),
                "updated_at": None}
        cols.update(kw)
        names = ", ".join(cols)
        qs = ", ".join("?" for _ in cols)
        self._conn.execute(
            f"INSERT INTO videos ({names}) VALUES ({qs})", tuple(cols.values())
        )
        self._conn.commit()

    def add_job(self, video_id, status="running"):
        self._conn.execute(
            "INSERT INTO generation_jobs (video_id, status) VALUES (?, ?)",
            (video_id, status),
        )
        self._conn.commit()


def _write(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _age_paths(*paths: Path, hours_ago=48):
    stamp = time.time() - hours_ago * 3600
    for p in paths:
        if p.is_dir():
            for f in p.rglob("*"):
                os.utime(f, (stamp, stamp))  # files + subdirs
            os.utime(p, (stamp, stamp))
        elif p.is_file():
            os.utime(p, (stamp, stamp))


@pytest.fixture()
def videos_root(tmp_path, monkeypatch):
    """Re-root videos_root (non-recursive) and segments at the tmp tree."""
    root = tmp_path / "videos"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setitem(mr.SWEEP_CATEGORIES, "videos_root", (root, False))
    seg = root / "segments"
    seg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setitem(mr.SWEEP_CATEGORIES, "segments", (seg, True))
    return root


@pytest.fixture()
def seg_root(videos_root):
    return videos_root / "segments"


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


def test_asset_history_unique_paths_mark_without_clearing(tmp_path, db):
    """Regresión: la tabla tiene UNIQUE(file_path); purgar no debe vaciar la
    ruta (violaría el constraint) sino solo sellar purged_at."""
    a1 = _write(tmp_path / "output" / "images" / "u1.jpg")
    a2 = _write(tmp_path / "output" / "images" / "u2.jpg")
    db._connect().executescript(f"""
        INSERT INTO videos (id, yt_video_id, status) VALUES (25, 'YT25', 'published');
        INSERT INTO video_asset_history (video_id, file_path) VALUES
            (25, '{a1}'), (25, '{a2}');
    """)
    db._connect().commit()

    report = mr.purge_entity_media(db, "video", 25)
    assert not report["errors"], report["errors"]
    rows = db._connect().execute(
        "SELECT file_path, purged_at FROM video_asset_history WHERE video_id=25"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["purged_at"] for r in rows)
    assert all(r["file_path"] for r in rows), "file_path debe conservarse"
    assert not a1.exists() and not a2.exists()

    # Los refs purgados ya no cuentan como referencia viva.
    refs = mr.build_reference_index(db)
    assert "u1.jpg" not in refs["uploaded_refs"]


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


# ── segments (fix fuga oct 2026) ─────────────────────────────────

def test_segments_dir_removed_after_upload_via_purge_entity(seg_root, db):
    """(a) El borrado en la fuente: tras subir, el dir de segments se va."""
    mp4 = _write(seg_root.parent / "final.mp4")
    s0 = _write(seg_root / "7" / "scene_0000.mp4", 4096)
    s1 = _write(seg_root / "7" / "scene_0001.mp4", 4096)
    staging = _write(seg_root / "7" / "_dist_staging_v2" / "scene_0000" / "asset.jpg",
                     2048)
    db.add_video(id=7, video_path=str(mp4), yt_video_id="YT7", status="published")

    report = mr.purge_entity_media(db, "video", 7)

    assert not s0.exists() and not s1.exists()
    assert not staging.exists(), "subdirs de staging son intermedios reclamables"
    assert not (seg_root / "7").exists(), "el dir vacio debe desaparecer"
    assert report["freed_bytes"] >= 4096 * 2 + 2048
    assert not report["errors"]

    # (f) idempotencia en la fuente
    second = mr.purge_entity_media(db, "video", 7)
    assert second["freed_bytes"] == 0
    assert not second["errors"]


def test_pending_video_segments_preserved(seg_root, db):
    """(b) Vídeo NO subido → el checkpoint de reassembly se respeta."""
    d = seg_root / "8"
    f = _write(d / "scene_0000.mp4", 4096)
    _age_paths(d, hours_ago=120)  # viejo, pero la entidad esta pendiente
    db.add_video(id=8, yt_video_id=None, status="awaiting_upload")

    plan = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    info = plan["categories"]["segments"]
    assert info["dirs_total"] == 1
    assert info["dirs_reclaim"] == 0
    assert info["reasons"].get("video_pending") == 1
    assert info["reclaim_bytes"] == 0
    assert info["protected_bytes"] == 4096

    rep = mr.purge_orphans(db, ["segments"], min_age_hours=24, dry_run=False)
    assert rep["freed_bytes"] == 0
    assert f.exists() and d.exists()


def test_old_orphan_segments_removed_recent_preserved(seg_root, db):
    """(c) Sin fila de vídeo: viejo = reclaim, reciente = preserve."""
    old = seg_root / "901"
    _write(old / "scene_0000.mp4", 2048)
    _age_paths(old, hours_ago=72)
    recent = seg_root / "902"
    r_file = _write(recent / "scene_0000.mp4", 2048)  # mtime = ahora
    empty_old = seg_root / "903"
    empty_old.mkdir()
    _age_paths(empty_old, hours_ago=72)

    plan = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    info = plan["categories"]["segments"]
    assert info["reclaim_reasons"].get("orphan_stale") == 2  # 901 + dir vacio 903
    assert info["reasons"].get("orphan_recent") == 1

    rep = mr.purge_orphans(db, ["segments"], min_age_hours=24, dry_run=False)
    assert rep["freed_bytes"] == 2048
    assert not old.exists() and not empty_old.exists()
    assert rep["categories"]["segments"]["dirs_removed"] == 2
    assert r_file.exists() and recent.exists()


def test_shared_asset_inside_reclaimable_dir_preserved(seg_root, db):
    """(d) Nombre referenciado por entidad pendiente → dir huérfano intacto."""
    d = seg_root / "904"
    a = _write(d / "scene_0000.mp4", 2048)
    b = _write(d / "shared.png", 2048)
    _age_paths(d, hours_ago=72)
    db.add_video(id=9, yt_video_id=None, status="awaiting_upload")
    db._conn.execute(
        "INSERT INTO video_scenes (video_id, image_path) VALUES (9, ?)",
        ("/root/autotube/output/images/shared.png",),
    )
    db._conn.commit()

    plan = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    assert plan["categories"]["segments"]["reasons"].get("orphan_protected") == 1

    rep = mr.purge_orphans(db, ["segments"], min_age_hours=24, dry_run=False)
    assert rep["freed_bytes"] == 0
    assert a.exists() and b.exists() and d.exists(), "preserve conservador del dir"


def test_locked_and_error_recent_preserved(seg_root, db):
    """(e) media_file_locks + ventana de reensamblado (48h)."""
    d_lock = seg_root / "11"
    f_lock = _write(d_lock / "scene_0000.mp4", 2048)
    _age_paths(d_lock, hours_ago=72)
    db.locked = {str(f_lock)}

    d_err = seg_root / "12"
    # NOTE: locks are keyed by BASENAME (by design, shared-safe). Use a
    # distinct scene name so the error-window rule is what we observe.
    f_err = _write(d_err / "scene_7777.mp4", 2048)
    _age_paths(d_err, hours_ago=72)
    db.add_video(id=12, yt_video_id=None, status="error",
                 created_at=_ts(hours_ago=6))  # dentro de la ventana

    plan = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    reasons = plan["categories"]["segments"]["reasons"]
    assert reasons.get("orphan_locked") == 1
    assert reasons.get("video_error_recent") == 1

    rep = mr.purge_orphans(db, ["segments"], min_age_hours=24, dry_run=False)
    assert rep["freed_bytes"] == 0
    assert f_lock.exists() and f_err.exists()


def test_active_job_uploaded_video_still_preserved(seg_root, db):
    """Job running/queued sobre vídeo subido → ni el sweep toca el dir."""
    d = seg_root / "14"
    f = _write(d / "scene_0000.mp4", 2048)
    db.add_video(id=14, yt_video_id="YT14", status="published")
    db.add_job(14, "running")

    plan = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    assert plan["categories"]["segments"]["reasons"].get("video_active_job") == 1
    rep = mr.purge_orphans(db, ["segments"], min_age_hours=24, dry_run=False)
    assert rep["freed_bytes"] == 0
    assert f.exists()


def test_non_numeric_dir_is_preserved(seg_root, db):
    d = seg_root / "adhoc-render"
    f = _write(d / "scene_0000.mp4", 2048)
    _age_paths(d, hours_ago=240)

    plan = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    assert plan["categories"]["segments"]["reasons"].get("non_numeric_dir") == 1
    rep = mr.purge_orphans(db, ["segments"], min_age_hours=24, dry_run=False)
    assert rep["freed_bytes"] == 0 and f.exists()


def test_uploaded_and_orphan_dirs_full_sweep_idempotent(seg_root, db):
    """(f) Dos pasadas: la segunda libera 0, sin errores, dirs vacios fuera."""
    up = seg_root / "20"
    _write(up / "scene_0000.mp4", 4096)
    _age_paths(up, hours_ago=72)
    db.add_video(id=20, yt_video_id="YT20", status="published")
    orph = seg_root / "920"
    _write(orph / "scene_0000.mp4", 1024)
    _age_paths(orph, hours_ago=72)

    first = mr.purge_orphans(db, ["segments"], min_age_hours=24, dry_run=False)
    assert first["freed_bytes"] == 4096 + 1024
    assert not up.exists() and not orph.exists()

    second = mr.purge_orphans(db, ["segments"], min_age_hours=24, dry_run=False)
    assert second["freed_bytes"] == 0
    assert not second["errors"]
    assert not any(seg_root.iterdir()), "ningun dir residuo"


def test_classify_manifest_segments_schema_keys(seg_root, db):
    up = seg_root / "21"
    _write(up / "scene_0000.mp4", 100)
    _age_paths(up, hours_ago=72)
    db.add_video(id=21, yt_video_id="YT21", status="published")
    pend = seg_root / "22"
    _write(pend / "scene_0000.mp4", 50)
    db.add_video(id=22, yt_video_id=None, status="generating")

    plan = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    info = plan["categories"]["segments"]
    # claves nuevas (observability oct 2026)
    for key in ("dirs_total", "dirs_reclaim", "files_total", "files_reclaim",
                "bytes_total", "reclaim_bytes", "protected_bytes", "reasons",
                "reclaim_reasons"):
        assert key in info, key
    # claves legacy intactas (schema backward-compatible)
    for key in ("root", "files", "bytes", "reclaim_files", "reclaim_bytes"):
        assert key in info, key
    assert info["dirs_total"] == 2 and info["dirs_reclaim"] == 1
    assert info["bytes_total"] == 150 and info["reclaim_bytes"] == 100
    assert info["protected_bytes"] == 50
    assert plan["stale_pending_entities"]["reclaimable_if_reconciled_bytes"] == 0


def test_stale_pending_entities_report_only(seg_root, videos_root, db):
    """(3) Filas muertas que protegen material → informe, cero borrado."""
    dead_mp4 = _write(videos_root / "old_final.mp4", 5000)
    _age_paths(dead_mp4, hours_ago=240)
    db.add_video(id=60, video_path=str(dead_mp4), yt_video_id=None,
                 status="discarded")
    sd = seg_root / "60"
    sf = _write(sd / "scene_0000.mp4", 3000)
    _age_paths(sd, hours_ago=240)
    # entidad viva (no debe aparecer en el informe)
    live = _write(videos_root / "live_final.mp4", 4000)
    db.add_video(id=61, video_path=str(live), yt_video_id=None,
                 status="awaiting_upload")

    plan = mr.classify_reclaim(db, ["videos_root", "segments"],
                               min_age_hours=24)
    stale = plan["stale_pending_entities"]
    by_id = {e["id"]: e for e in stale["entities"]}
    assert 60 in by_id and 61 not in by_id
    e = by_id[60]
    assert e["status"] == "discarded"
    assert e["files"] == 1 and e["bytes"] == 5000
    assert e["segments_dir"].endswith("/60")
    assert e["segments_bytes"] == 3000
    assert stale["reclaimable_if_reconciled_bytes"] == 8000

    # REPORT-ONLY: tras el classify el material sigue intacto
    assert dead_mp4.exists() and sf.exists()
    assert plan["categories"]["segments"]["reasons"].get(
        "video_terminal_dead") == 1


def test_stale_pending_error_stale_row_reported(seg_root, db):
    """Error > 48h sin job: preserved por el sweep, listado para reconciliar."""
    d = seg_root / "70"
    f = _write(d / "scene_0000.mp4", 1500)
    _age_paths(d, hours_ago=240)
    db.add_video(id=70, yt_video_id=None, status="error",
                 created_at=_ts(days_ago=10))

    plan = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    assert plan["categories"]["segments"]["reasons"].get("video_error_stale") == 1
    stale = plan["stale_pending_entities"]
    assert any(e["id"] == 70 and e["segments_bytes"] == 1500
               for e in stale["entities"])
    assert f.exists()


def test_files_reclaim_counts_only_reclaimed_dirs(seg_root, db):
    """Regresión: preserved dirs no inflan files_reclaim del manifiesto."""
    pend = seg_root / "30"
    _write(pend / "scene_0000.mp4", 10)
    _write(pend / "scene_0001.mp4", 10)
    db.add_video(id=30, yt_video_id=None, status="awaiting_upload")
    old = seg_root / "930"
    _write(old / "scene_0000.mp4", 10)
    _age_paths(old, hours_ago=72)

    info = mr.classify_reclaim(db, ["segments"], min_age_hours=24)
    info = info["categories"]["segments"]
    assert info["files_total"] == 3
    assert info["files_reclaim"] == 1 and info["reclaim_files"] == 1
    assert info["reclaim_bytes"] == 10
    assert info["protected_bytes"] == 20
    assert info["dirs_total"] == 2 and info["dirs_reclaim"] == 1
