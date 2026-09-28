"""Media retention — 0-day cleanup of generation material after a confirmed upload.

Requirement (sep 2026): as soon as a video/short is **successfully uploaded** to
YouTube, its heavy generation material must be deleted from local disk:

  * the final MP4 (and any SEO-renamed copy left behind),
  * narration MP3 + CTA audio and their derived timestamps/SRT,
  * scene assets (images / video clips / AI scenes) + ``.pollo.json`` sidecars,
  * short assets (native/clip) tracked in ``short_asset_history``.

Preserved on purpose:
  * thumbnails (used by the panel),
  * main narration SRT + ``_timestamps.json`` (SEO / chapters / analysis).

Safety rules (never relaxed):
  * never delete material of an entity that is NOT confirmed uploaded,
  * never delete a file locked by an active job (``media_file_locks``),
  * never delete a shared asset still referenced by a non-uploaded entity,
  * never delete a recent file (< ``min_age_hours``) during the orphan sweep.

DB consistency: rows are kept and stamped with ``purged_at`` /
``media_purged_at``; dangling path columns are cleared. ``asset_url`` is
preserved so cross-video dedup keeps working without the local file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

from config import settings as _settings

PROJECT_ROOT = _settings.PROJECT_ROOT
OUTPUT_DIR = _settings.OUTPUT_DIR

# ── Uploaded (confirmed) states ──────────────────────────────────
# A long-form is considered uploaded when YouTube assigned it an id. The
# statuses below are the historical "uploaded" states; the yt id is the real
# signal and is checked independently.
UPLOADED_VIDEO_STATUSES = {
    "published",
    "uploaded",
    "uploaded_private",
    "deleted_on_yt",
    "unlisted",
    "private_quality_issue",
}
UPLOADED_SHORT_STATUSES = {"published", "scheduled"}

# Extensions / suffixes that are preserved during the orphan sweep (main SEO
# subtitles + chapter timestamps). Entity purge handles CTA-derived files
# explicitly.
PRESERVED_SUFFIXES = ("_subtitles.srt", "_timestamps.json")
PRESERVED_EXTENSIONS = (".srt",)

# Orphan sweep categories → (root path, recursive). ``videos_root`` only
# scans immediate files because its subdirectories are separate categories.
SWEEP_CATEGORIES: dict[str, tuple[Path, bool]] = {
    "videos_root": (OUTPUT_DIR / "videos", False),
    "shorts_clips": (OUTPUT_DIR / "videos" / "shorts_clips", True),
    "shorts": (OUTPUT_DIR / "videos" / "shorts", True),
    "images": (OUTPUT_DIR / "images", True),
    "ai_images": (OUTPUT_DIR / "ai_images", True),
    "ai_cache_pollinations": (OUTPUT_DIR / "ai_cache" / "pollinations", True),
    "audio": (OUTPUT_DIR / "audio", True),
    "ai_scenes": (OUTPUT_DIR / "ai_scenes", True),
    "temp": (OUTPUT_DIR / "temp", True),
}
# Never swept: output/thumbnails (panel), output/videos/shorts_clips handled by
# category, output/video_clips cleaned by the pipeline preflight.


# ──────────────────────────────────────────────────────────────────
# Path / reference helpers
# ──────────────────────────────────────────────────────────────────

def normalize_basename(value) -> str:
    """Extract the file basename from any DB representation.

    Handles plain paths, ``PosixPath('...')`` repos and dict-reprs like
    ``{'path': 'output/x/y.jpg', ...}`` produced by legacy code.
    """
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if s.startswith("{"):
        match = re.search(r"PosixPath\('([^']+)'\)", s)
        if match:
            return os.path.basename(match.group(1))
        match = re.search(r"'path'\s*:\s*'([^']+)'", s)
        if match:
            return os.path.basename(match.group(1))
        return ""
    return os.path.basename(s)


def normalize_path(value) -> str:
    """Return a usable filesystem path from a DB representation (or '')."""
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if s.startswith("{"):
        match = re.search(r"PosixPath\('([^']+)'\)", s)
        if match:
            return match.group(1)
        match = re.search(r"'path'\s*:\s*'([^']+)'", s)
        if match:
            return match.group(1)
        return ""
    return s


def _abs_path(path_value) -> Optional[Path]:
    """Resolve a DB path to an absolute Path.

    Relative paths starting with ``output/`` are rebased onto the configured
    output directory (so running from a worktree / with ``OUTPUT_DIR`` override
    still targets the real data). Other relative paths resolve to PROJECT_ROOT.
    """
    p = normalize_path(path_value)
    if not p:
        return None
    path = Path(p)
    if path.is_absolute():
        return path
    posix = path.as_posix()
    if posix.startswith("output/"):
        return OUTPUT_DIR.parent / posix
    return PROJECT_ROOT / posix


def _is_preserved_path(path: Path) -> bool:
    """Files that must never be swept (thumbnails + main SEO subtitles)."""
    if "output/thumbnails" in path.as_posix():
        return True
    name = path.name.lower()
    if name.endswith(PRESERVED_SUFFIXES):
        return True
    if name.endswith(PRESERVED_EXTENSIONS):
        return True
    return False


# ──────────────────────────────────────────────────────────────────
# Reference index + safety sets
# ──────────────────────────────────────────────────────────────────

def _iter_reference_rows(db) -> Iterable[tuple[str, bool]]:
    """Yield ``(basename, is_uploaded)`` for every referenced media file.

    Long-form status is derived from ``yt_video_id``; short status from
    ``youtube_id``. History/scene rows inherit the status of their parent.
    """
    conn = db._connect()

    # Videos: video_path / audio_path / thumbnail_path
    for row in conn.execute(
        "SELECT id, video_path, audio_path, thumbnail_path, "
        "(yt_video_id IS NOT NULL AND yt_video_id != '') AS uploaded "
        "FROM videos"
    ):
        uploaded = bool(row["uploaded"])
        for col in ("video_path", "audio_path", "thumbnail_path"):
            bn = normalize_basename(row[col])
            if bn:
                yield bn, uploaded

    # Scenes: image_path / audio_path inherit the parent video status
    for row in conn.execute(
        "SELECT s.image_path, s.audio_path, "
        "(v.yt_video_id IS NOT NULL AND v.yt_video_id != '') AS uploaded "
        "FROM video_scenes s LEFT JOIN videos v ON s.video_id = v.id"
    ):
        uploaded = bool(row["uploaded"])
        for col in ("image_path", "audio_path"):
            bn = normalize_basename(row[col])
            if bn:
                yield bn, uploaded

    # video_asset_history inherits the parent video status
    for row in conn.execute(
        "SELECT h.file_path, "
        "(v.yt_video_id IS NOT NULL AND v.yt_video_id != '') AS uploaded "
        "FROM video_asset_history h LEFT JOIN videos v ON h.video_id = v.id"
    ):
        bn = normalize_basename(row["file_path"])
        if bn:
            yield bn, bool(row["uploaded"])

    # short_asset_history inherits the parent short status
    for row in conn.execute(
        "SELECT h.file_path, "
        "(s.youtube_id IS NOT NULL AND s.youtube_id != '') AS uploaded "
        "FROM short_asset_history h LEFT JOIN shorts s ON h.short_id = s.id"
    ):
        bn = normalize_basename(row["file_path"])
        if bn:
            yield bn, bool(row["uploaded"])

    # Shorts: file_path / thumbnail_path
    for row in conn.execute(
        "SELECT file_path, thumbnail_path, "
        "(youtube_id IS NOT NULL AND youtube_id != '') AS uploaded FROM shorts"
    ):
        uploaded = bool(row["uploaded"])
        for col in ("file_path", "thumbnail_path"):
            bn = normalize_basename(row[col])
            if bn:
                yield bn, uploaded


def build_reference_index(db) -> dict:
    """Build the basename reference sets used by every purge decision.

    Returns a dict with:
      * ``all_refs``: every referenced basename,
      * ``uploaded_refs``: basenames referenced by a confirmed-uploaded entity,
      * ``protected_refs``: basenames referenced by a NON-uploaded entity
        (these must never be deleted while that entity is pending).
    """
    all_refs: set[str] = set()
    uploaded_refs: set[str] = set()
    protected_refs: set[str] = set()
    for bn, uploaded in _iter_reference_rows(db):
        all_refs.add(bn)
        if uploaded:
            uploaded_refs.add(bn)
        else:
            protected_refs.add(bn)
    return {
        "all_refs": all_refs,
        "uploaded_refs": uploaded_refs,
        "protected_refs": protected_refs,
    }


def _locked_basenames(db) -> set[str]:
    """Basenames of files locked by active jobs (+ recent error reassembly)."""
    names: set[str] = set()
    try:
        for fp in db.get_locked_file_paths():
            names.add(os.path.basename(str(fp)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("media_retention: could not read media_file_locks: %s", exc)
    try:
        for fp in db.get_error_video_media_paths(max_age_hours=48):
            names.add(os.path.basename(str(fp)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("media_retention: could not read error media paths: %s", exc)
    return names


def _entity_uploaded(db, kind: str, entity_id: int) -> bool:
    conn = db._connect()
    if kind == "video":
        row = conn.execute(
            "SELECT yt_video_id FROM videos WHERE id = ?", (entity_id,)
        ).fetchone()
        return bool(row and row["yt_video_id"])
    row = conn.execute(
        "SELECT youtube_id FROM shorts WHERE id = ?", (entity_id,)
    ).fetchone()
    return bool(row and row["youtube_id"])


# ──────────────────────────────────────────────────────────────────
# Deletion primitives
# ──────────────────────────────────────────────────────────────────

def _unlink(path: Optional[Path], report: dict, label: str, dry_run: bool,
            locked: set[str]) -> int:
    """Delete a file (honouring locks). Returns bytes freed. Logs failures."""
    if path is None:
        return 0
    bn = path.name
    if bn in locked:
        report["skipped_locked"].append(str(path))
        return 0
    try:
        if not path.is_file():
            return 0
        size = path.stat().st_size
        if dry_run:
            report["would_delete"].append(str(path))
            return size
        path.unlink()
        report["deleted"].append(str(path))
        return size
    except Exception as exc:  # noqa: BLE001 — never silence a real failure
        report["errors"].append({"path": str(path), "error": str(exc)})
        logger.warning("media_retention: could not delete %s %s: %s", label, path, exc)
        return 0


def _mark_purged(db, sql: str, params: tuple, report: dict, dry_run: bool,
                 label: str) -> None:
    if dry_run:
        return
    try:
        with db._connect() as conn:
            conn.execute(sql, params)
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        report["errors"].append({"db": label, "error": str(exc)})
        logger.warning("media_retention: DB mark failed (%s): %s", label, exc)


# ──────────────────────────────────────────────────────────────────
# Entity purge (post-upload, 0-day retention)
# ──────────────────────────────────────────────────────────────────

def purge_entity_media(db, kind: str, entity_id: int, reason: str = "upload",
                       dry_run: bool = False, log=None,
                       refs: Optional[dict] = None,
                       locked: Optional[set] = None) -> dict:
    """Purge the heavy material of a single uploaded video/short.

    Idempotent and safe: returns immediately if the entity is not confirmed
    uploaded. Returns a report dict with freed bytes and per-action detail.

    ``refs`` / ``locked`` may be supplied by a batch caller to avoid rebuilding
    the (80k-row) reference index for every entity.
    """
    _log = log or logger
    report: dict = {
        "kind": kind,
        "id": entity_id,
        "reason": reason,
        "dry_run": dry_run,
        "freed_bytes": 0,
        "deleted": [],
        "would_delete": [],
        "skipped_locked": [],
        "skipped_protected": [],
        "errors": [],
    }

    if not _entity_uploaded(db, kind, entity_id):
        report["skipped"] = "not_uploaded"
        return report

    if refs is None:
        refs = build_reference_index(db)
    protected = refs["protected_refs"]
    if locked is None:
        locked = _locked_basenames(db)

    if kind == "video":
        _purge_video(db, entity_id, report, protected, locked, dry_run, _log)
    elif kind == "short":
        _purge_short(db, entity_id, report, protected, locked, dry_run, _log)
    else:
        report["errors"].append({"error": f"unknown kind {kind}"})
        return report

    if not dry_run:
        _log.info(
            "media_retention: purged %s #%d (%s) — freed %.1f MB, deleted %d files",
            kind, entity_id, reason,
            report["freed_bytes"] / (1024 * 1024),
            len(report["deleted"]),
        )
    return report


def _purge_video(db, video_id: int, report: dict, protected: set[str],
                 locked: set[str], dry_run: bool, _log) -> None:
    video = db.get_video(video_id)
    if not video:
        report["errors"].append({"error": "video not found"})
        return

    def _del(value, label, guard_protected=True):
        p = _abs_path(value)
        if p is None:
            return
        if guard_protected and p.name in protected:
            report["skipped_protected"].append(str(p))
            return
        report["freed_bytes"] += _unlink(p, report, label, dry_run, locked)

    # ── MP4 ───────────────────────────────────────────────────────
    _del(video.get("video_path"), "MP4")

    # ── Main narration MP3 (keep the main SRT/timestamps) ────────
    audio_path = video.get("audio_path")
    _del(audio_path, "main MP3")

    # ── CTA audio + its derived timestamps/SRT ───────────────────
    cta_path = ""
    cp_raw = video.get("checkpoint_data") or "{}"
    try:
        cp = json.loads(cp_raw) if isinstance(cp_raw, str) else (cp_raw or {})
        cta_path = (cp.get("tts") or {}).get("cta_audio_path", "")
    except (json.JSONDecodeError, TypeError):
        pass
    if cta_path:
        _del(cta_path, "CTA MP3")
        base = _abs_path(cta_path)
        if base is not None:
            for suffix in ("_timestamps.json", "_subtitles.srt"):
                _del(str(base.parent / f"{base.stem}{suffix}"), "CTA derived")

    # ── Scene assets + .pollo.json sidecars ──────────────────────
    try:
        scenes = db.get_scenes(video_id)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append({"error": f"get_scenes: {exc}"})
        scenes = []
    for scene in scenes:
        for value in (scene.get("image_path"), scene.get("audio_path")):
            raw = normalize_path(value)
            if not raw:
                continue
            _del(value, "scene asset")
            p = _abs_path(value)
            if p is not None and p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                _del(str(p.with_suffix(".pollo.json")), "AI sidecar", guard_protected=False)

    # ── Assets recorded in history for this video ────────────────
    _purge_asset_history(db, "video_asset_history", "video_id", video_id,
                         report, protected, locked, dry_run)

    # ── Mark DB (keep rows; clear dangling paths) ────────────────
    _mark_purged(
        db,
        "UPDATE video_scenes SET purged_at=datetime('now'), image_path='', audio_path='' "
        "WHERE video_id=? AND purged_at IS NULL",
        (video_id,), report, dry_run, "video_scenes",
    )
    _mark_purged(
        db,
        "UPDATE videos SET media_purged_at=datetime('now'), video_path='' WHERE id=?",
        (video_id,), report, dry_run, "videos",
    )


def _purge_short(db, short_id: int, report: dict, protected: set[str],
                 locked: set[str], dry_run: bool, _log) -> None:
    short = db.get_short(short_id)
    if not short:
        report["errors"].append({"error": "short not found"})
        return

    def _del(value, label):
        p = _abs_path(value)
        if p is None:
            return
        if p.name in protected:
            report["skipped_protected"].append(str(p))
            return
        report["freed_bytes"] += _unlink(p, report, label, dry_run, locked)

    _del(short.get("file_path"), "short MP4")
    # Thumbnail preserved on purpose.

    # ── Short assets recorded in history ─────────────────────────
    _purge_asset_history(db, "short_asset_history", "short_id", short_id,
                         report, protected, locked, dry_run)

    _mark_purged(
        db,
        "UPDATE shorts SET media_purged_at=datetime('now'), file_path='' WHERE id=?",
        (short_id,), report, dry_run, "shorts",
    )


def _purge_asset_history(db, table: str, fk_col: str, entity_id: int,
                         report: dict, protected: set[str], locked: set[str],
                         dry_run: bool) -> None:
    """Delete files referenced by an entity's asset-history rows (shared-safe)."""
    try:
        rows = db._connect().execute(
            f"SELECT file_path FROM {table} WHERE {fk_col} = ?",
            (entity_id,),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        report["errors"].append({"error": f"{table}: {exc}"})
        return
    for row in rows:
        value = row["file_path"]
        p = _abs_path(value)
        if p is None:
            continue
        if p.name in protected:
            report["skipped_protected"].append(str(p))
            continue
        report["freed_bytes"] += _unlink(p, report, table, dry_run, locked)
    _mark_purged(
        db,
        f"UPDATE {table} SET purged_at=datetime('now'), file_path='' "
        f"WHERE {fk_col}=? AND purged_at IS NULL",
        (entity_id,), report, dry_run, table,
    )


# ──────────────────────────────────────────────────────────────────
# Orphan sweep (retroactive + periodic)
# ──────────────────────────────────────────────────────────────────

def classify_reclaim(db, categories: Optional[Iterable[str]] = None,
                     min_age_hours: float = 24.0) -> dict:
    """Compute (without deleting) what the sweep would reclaim.

    A file is reclaimable when it is NOT referenced by any non-uploaded
    entity, is NOT locked, is older than ``min_age_hours`` and is not a
    preserved artifact (thumbnail / main SEO subtitle).
    """
    refs = build_reference_index(db)
    protected = refs["protected_refs"]
    locked = _locked_basenames(db)
    cutoff = time.time() - min_age_hours * 3600.0

    cats = list(categories) if categories else list(SWEEP_CATEGORIES)
    result = {
        "min_age_hours": min_age_hours,
        "protected_refs": len(protected),
        "locked": len(locked),
        "categories": {},
        "total_bytes": 0,
        "reclaim_bytes": 0,
        "files_total": 0,
        "files_reclaim": 0,
        "errors": [],
    }

    for cat in cats:
        spec = SWEEP_CATEGORIES.get(cat)
        if not spec:
            result["errors"].append({"error": f"unknown category {cat}"})
            continue
        root, recursive = spec
        cat_info = {"root": str(root), "files": 0, "bytes": 0,
                    "reclaim_files": 0, "reclaim_bytes": 0, "reasons": {}}
        if root.is_dir():
            it = root.rglob("*") if recursive else root.iterdir()
            for path in it:
                try:
                    if not path.is_file():
                        continue
                    size = path.stat().st_size
                except OSError:
                    continue
                cat_info["files"] += 1
                cat_info["bytes"] += size
                reason = _reclaim_reason(path, protected, locked, cutoff)
                if reason is None:
                    cat_info["reclaim_files"] += 1
                    cat_info["reclaim_bytes"] += size
                else:
                    cat_info["reasons"][reason] = cat_info["reasons"].get(reason, 0) + 1
        result["categories"][cat] = cat_info
        result["total_bytes"] += cat_info["bytes"]
        result["reclaim_bytes"] += cat_info["reclaim_bytes"]
        result["files_total"] += cat_info["files"]
        result["files_reclaim"] += cat_info["reclaim_files"]

    return result


def _reclaim_reason(path: Path, protected: set[str], locked: set[str],
                    cutoff: float) -> Optional[str]:
    """Return None if the file can be reclaimed, else the preservation reason."""
    if path.name in locked:
        return "locked"
    if _is_preserved_path(path):
        return "preserved_artifact"
    if path.name in protected:
        return "referenced_by_pending"
    try:
        if path.stat().st_mtime > cutoff:
            return "too_recent"
    except OSError:
        return "stat_error"
    return None


def plan_reclaim_paths(db, categories: Optional[Iterable[str]] = None,
                       min_age_hours: float = 24.0) -> dict[str, int]:
    """Return ``{absolute_path: size}`` reclaimable by the orphan sweep.

    Pure dry-run: never deletes. Used to build an honest, de-duplicated
    reclamation manifest together with the entity-purge candidate lists.
    """
    refs = build_reference_index(db)
    protected = refs["protected_refs"]
    locked = _locked_basenames(db)
    cutoff = time.time() - min_age_hours * 3600.0
    cats = list(categories) if categories else list(SWEEP_CATEGORIES)
    out: dict[str, int] = {}
    for cat in cats:
        spec = SWEEP_CATEGORIES.get(cat)
        if not spec:
            continue
        root, recursive = spec
        if not root.is_dir():
            continue
        it = root.rglob("*") if recursive else root.iterdir()
        for path in it:
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            if _reclaim_reason(path, protected, locked, cutoff) is None:
                out[str(path)] = size
    return out


def purge_orphans(db, categories: Optional[Iterable[str]] = None,
                  min_age_hours: float = 24.0, dry_run: bool = True,
                  log=None) -> dict:
    """Delete reclaimable files per :func:`classify_reclaim`.

    ``dry_run`` defaults to True so a caller must opt in to real deletion.
    """
    _log = log or logger
    refs = build_reference_index(db)
    protected = refs["protected_refs"]
    locked = _locked_basenames(db)
    cutoff = time.time() - min_age_hours * 3600.0

    cats = list(categories) if categories else list(SWEEP_CATEGORIES)
    report = {"dry_run": dry_run, "min_age_hours": min_age_hours,
              "freed_bytes": 0, "deleted": [], "would_delete": [],
              "errors": [], "categories": {}}

    for cat in cats:
        spec = SWEEP_CATEGORIES.get(cat)
        if not spec:
            report["errors"].append({"error": f"unknown category {cat}"})
            continue
        root, recursive = spec
        cat_freed = 0
        cat_files = 0
        if root.is_dir():
            it = root.rglob("*") if recursive else root.iterdir()
            for path in it:
                if not path.is_file():
                    continue
                if _reclaim_reason(path, protected, locked, cutoff) is not None:
                    continue
                freed = _unlink(path, report, cat, dry_run, locked)
                if freed:
                    cat_freed += freed
                    cat_files += 1
        report["categories"][cat] = {"freed_bytes": cat_freed, "files": cat_files}
        report["freed_bytes"] += cat_freed

    if not dry_run:
        _log.info(
            "media_retention: orphan sweep freed %.2f GB across %s",
            report["freed_bytes"] / (1024 ** 3), cats,
        )
    return report
