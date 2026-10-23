"""Media retention — 0-day cleanup of generation material after a confirmed upload.

Requirement (sep 2026): as soon as a video/short is **successfully uploaded** to
YouTube, its heavy generation material must be deleted from local disk:

  * the final MP4 (and any SEO-renamed copy left behind),
  * narration MP3 + CTA audio and their derived timestamps/SRT,
  * scene assets (images / video clips / AI scenes) + ``.pollo.json`` sidecars,
  * scene render segments (``output/videos/segments/<video_id>/`` — dir-level,
    oct 2026 leak fix),
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
from datetime import datetime, timezone
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
#
# Coverage audit (oct 2026) — every immediate subdir of ``output/videos``:
#   * shorts        → category (file-level, recursive)
#   * shorts_clips  → category (file-level, recursive)
#   * segments      → category (DIRECTORY-level — see DIR_SWEEP_CATEGORIES)
#   * canal5/A/B    → empty legacy dirs (0 bytes on disk); never swept —
#                     if they ever receive files they must get their own
#                     category, because videos_root is non-recursive.
SWEEP_CATEGORIES: dict[str, tuple[Path, bool]] = {
    "videos_root": (OUTPUT_DIR / "videos", False),
    "shorts_clips": (OUTPUT_DIR / "videos" / "shorts_clips", True),
    "shorts": (OUTPUT_DIR / "videos" / "shorts", True),
    "segments": (OUTPUT_DIR / "videos" / "segments", True),
    "images": (OUTPUT_DIR / "images", True),
    "ai_images": (OUTPUT_DIR / "ai_images", True),
    "ai_cache_pollinations": (OUTPUT_DIR / "ai_cache" / "pollinations", True),
    "audio": (OUTPUT_DIR / "audio", True),
    "ai_scenes": (OUTPUT_DIR / "ai_scenes", True),
    "temp": (OUTPUT_DIR / "temp", True),
}
# Never swept: output/thumbnails (panel), output/videos/canalX (empty legacy
# dirs), output/video_clips cleaned by the pipeline preflight.

# Categories classified at DIRECTORY granularity instead of per-file. The
# segments tree (``output/videos/segments/<video_id>/scene_NNNN.mp4``) is
# rendered intermediates keyed by numeric video id; the DB never references
# those basenames, so per-file orphan logic can't see the entity that owns
# them (or worse: would delete the checkpoint of a pending video). Rules are
# implemented in :func:`plan_segments_sweep`.
DIR_SWEEP_CATEGORIES = {"segments"}

# Reassembly grace: videos in error/retry keep their material for this window
# (mirrors ``get_error_video_media_paths(max_age_hours=48)``).
ERROR_LOCK_WINDOW_HOURS = 48.0

# Video states that will never produce an upload again (terminal). Material of
# rows in these states is PRESERVED by the sweep (the row still owns it) but is
# listed in the ``stale_pending_entities`` report so an operator can reconcile.
TERMINAL_DEAD_VIDEO_STATUSES = frozenset({"discarded", "cancelled", "validation_failed"})
DEAD_RETRY_VIDEO_STATUSES = frozenset({"error", "failed", "retrying"})
TERMINAL_DEAD_SHORT_STATUSES = frozenset({"cancelled", "error", "failed"})

# Non-uploaded rows untouched for longer than this are reported as stale
# pending candidates (report only — never auto-reconciled).
STALE_PENDING_DAYS = 45.0


def segments_root() -> Path:
    """Root of the per-video render segments (single source of truth)."""
    spec = SWEEP_CATEGORIES.get("segments")
    return spec[0] if spec else OUTPUT_DIR / "videos" / "segments"


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

def _iter_reference_rows(db) -> Iterable[tuple[str, bool, str, int]]:
    """Yield ``(basename, is_uploaded, kind, entity_id)`` for every referenced
    media file.

    Long-form status is derived from ``yt_video_id``; short status from
    ``youtube_id``. History/scene rows inherit the status of their parent.
    ``kind``/``entity_id`` identify the OWNING entity (video/scene/asset_hist
    → videos.id; short/short_asset → shorts.id) for the stale-pending report.
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
                yield bn, uploaded, "video", row["id"]

    # Scenes: image_path / audio_path inherit the parent video status
    for row in conn.execute(
        "SELECT s.image_path, s.audio_path, s.video_id, "
        "(v.yt_video_id IS NOT NULL AND v.yt_video_id != '') AS uploaded "
        "FROM video_scenes s LEFT JOIN videos v ON s.video_id = v.id"
    ):
        uploaded = bool(row["uploaded"])
        for col in ("image_path", "audio_path"):
            bn = normalize_basename(row[col])
            if bn:
                yield bn, uploaded, "scene", row["video_id"]

    # video_asset_history inherits the parent video status.
    # Purged rows are excluded: they keep file_path (no clearing — the table has
    # a UNIQUE constraint on file_path) and must not count as live references.
    for row in conn.execute(
        "SELECT h.file_path, h.video_id, "
        "(v.yt_video_id IS NOT NULL AND v.yt_video_id != '') AS uploaded "
        "FROM video_asset_history h LEFT JOIN videos v ON h.video_id = v.id "
        "WHERE h.purged_at IS NULL"
    ):
        bn = normalize_basename(row["file_path"])
        if bn:
            yield bn, bool(row["uploaded"]), "asset_hist", row["video_id"]

    # short_asset_history inherits the parent short status (purged excluded).
    for row in conn.execute(
        "SELECT h.file_path, h.short_id, "
        "(s.youtube_id IS NOT NULL AND s.youtube_id != '') AS uploaded "
        "FROM short_asset_history h LEFT JOIN shorts s ON h.short_id = s.id "
        "WHERE h.purged_at IS NULL"
    ):
        bn = normalize_basename(row["file_path"])
        if bn:
            yield bn, bool(row["uploaded"]), "short_asset", row["short_id"]

    # Shorts: file_path / thumbnail_path
    for row in conn.execute(
        "SELECT id, file_path, thumbnail_path, "
        "(youtube_id IS NOT NULL AND youtube_id != '') AS uploaded FROM shorts"
    ):
        uploaded = bool(row["uploaded"])
        for col in ("file_path", "thumbnail_path"):
            bn = normalize_basename(row[col])
            if bn:
                yield bn, uploaded, "short", row["id"]


def build_reference_index(db) -> dict:
    """Build the basename reference sets used by every purge decision.

    Returns a dict with:
      * ``all_refs``: every referenced basename,
      * ``uploaded_refs``: basenames referenced by a confirmed-uploaded entity,
      * ``protected_refs``: basenames referenced by a NON-uploaded entity
        (these must never be deleted while that entity is pending),
      * ``protected_rows``: ``{basename: [{"kind", "id"}, ...]}`` — the
        non-uploaded rows behind each protected basename (additive key; used
        only by the report-only stale-pending analysis).
    """
    all_refs: set[str] = set()
    uploaded_refs: set[str] = set()
    protected_refs: set[str] = set()
    protected_rows: dict[str, list[dict]] = {}
    for bn, uploaded, kind, rid in _iter_reference_rows(db):
        all_refs.add(bn)
        if uploaded:
            uploaded_refs.add(bn)
        else:
            protected_refs.add(bn)
            entries = protected_rows.setdefault(bn, [])
            entry = {"kind": kind, "id": rid}
            if entry not in entries:
                entries.append(entry)
    return {
        "all_refs": all_refs,
        "uploaded_refs": uploaded_refs,
        "protected_refs": protected_refs,
        "protected_rows": protected_rows,
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
# Segment-directory sweep (output/videos/segments/<video_id>)
# ──────────────────────────────────────────────────────────────────

def _parse_db_ts(value) -> Optional[float]:
    """Parse a SQLite datetime string (UTC) to epoch seconds, else None."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _active_video_ids(db) -> set:
    """video_ids referenced by a running/queued job (never swept)."""
    try:
        return {row["video_id"] for row in db._connect().execute(
            "SELECT DISTINCT video_id FROM generation_jobs "
            "WHERE status IN ('running','queued') AND video_id IS NOT NULL"
        )}
    except Exception as exc:  # noqa: BLE001 — missing table must not kill the sweep
        logger.warning("media_retention: could not read active jobs: %s", exc)
        return set()


def _segments_video_meta(db, ids: list) -> dict:
    """``{video_id: {status, yt_video_id, created_at, updated_at}}``.

    Degrades safely: a per-row query error yields a *pending-like* synthetic
    meta (→ dir preserved). Only a genuinely absent videos row maps to
    ``None`` here (→ orphan rules). A broken metadata query must never
    expose live material.
    """
    if not ids:
        return {}
    out: dict = {}
    conn = db._connect()
    for vid in ids:
        try:
            row = conn.execute(
                "SELECT id, status, yt_video_id, created_at, updated_at "
                "FROM videos WHERE id = ?", (vid,)
            ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("media_retention: segments meta #%s failed: %s", vid, exc)
            out[vid] = {"id": vid, "status": "awaiting_upload",
                        "yt_video_id": None, "created_at": None,
                        "updated_at": None}
            continue
        if row:
            out[vid] = dict(row)
    return out


def _segment_dir_files(dir_path: Path) -> tuple[list[tuple[Path, int]], float]:
    """Return ``([(path, size)], newest_mtime)`` for a segment directory."""
    files: list[tuple[Path, int]] = []
    newest = 0.0
    try:
        newest = dir_path.stat().st_mtime
    except OSError:
        return files, 0.0
    for path in sorted(dir_path.rglob("*")):
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
        files.append((path, size))
    return files, newest


def _classify_segment_dir(name: str, meta: Optional[dict], *, active: bool,
                          locked_hit: bool, protected_hit: bool,
                          newest_mtime: float, cutoff: float,
                          now: float) -> tuple[bool, str]:
    """Rules (evaluated in this order) → ``(reclaim, reason)``.

    Reclaim reasons: ``video_uploaded`` / ``orphan_stale``. Preserve reasons:
    ``non_numeric_dir``, ``video_active_job``, ``video_error_recent``,
    ``video_terminal_dead``, ``video_error_stale``, ``video_pending``,
    ``orphan_locked``, ``orphan_protected``, ``orphan_recent``.

    Conservative by construction: anything that cannot be proven reclaimable is
    preserved. Non-numeric dir names are never reclaimed (unknown owner).
    """
    numeric = name.isdigit()
    if not numeric:
        # Unknown name (e.g. `_dist_staging_v2` stray, adhoc render): always
        # preserved — reclaiming it could not be justified safely.
        return False, "non_numeric_dir"

    if meta is None:
        # No videos row at all → orphan dir: reclaim ONLY when old, no file
        # locked and no filename referenced by a pending entity.
        if locked_hit:
            return False, "orphan_locked"
        if protected_hit:
            return False, "orphan_protected"
        if newest_mtime > cutoff:
            return False, "orphan_recent"
        return True, "orphan_stale"

    if active or locked_hit:
        # A running/queued job owns this video (or files inside are locked by
        # one) — even for uploaded rows the sweep defers to the active job.
        return False, "video_active_job"

    uploaded = bool((meta.get("yt_video_id") or "").strip())
    if uploaded:
        # 0-day policy: material of a confirmed upload is reclaimable now
        # (per-file lock/protected checks still apply in the caller).
        return True, "video_uploaded"

    status = str(meta.get("status") or "").lower()
    if status in DEAD_RETRY_VIDEO_STATUSES:
        ts = _parse_db_ts(meta.get("updated_at")) or _parse_db_ts(meta.get("created_at"))
        age_h = (now - ts) / 3600.0 if ts else 0.0
        if age_h <= ERROR_LOCK_WINDOW_HOURS:
            return False, "video_error_recent"
        return False, "video_error_stale"
    if status in TERMINAL_DEAD_VIDEO_STATUSES:
        return False, "video_terminal_dead"
    return False, "video_pending"


def plan_segments_sweep(db, refs: dict, locked: set[str], cutoff: float,
                        *, now: Optional[float] = None) -> list[dict]:
    """Directory-granularity plan for ``output/videos/segments``.

    Pure read: returns one decision per child directory:
    ``{dir, path, numeric, video_id, reclaim, reason, files_total,
       bytes_total, file_actions: [(path, size)], excluded_files,
       excluded_bytes, can_rmdir}``.
    ``file_actions`` only contains the files a reclaim decision would delete —
    locked / preserved / pending-referenced names are always excluded.
    """
    now = time.time() if now is None else now
    root = segments_root()
    decisions: list[dict] = []
    if not root.is_dir():
        return decisions

    children = [d for d in sorted(root.iterdir()) if d.is_dir()]
    ids = [int(d.name) for d in children if d.name.isdigit()]
    metas = _segments_video_meta(db, ids)
    active = _active_video_ids(db)
    protected = refs.get("protected_refs", set())

    for d in children:
        files, newest = _segment_dir_files(d)
        locked_names = [p for p, _ in files if p.name in locked]
        excluded = [(p, s) for p, s in files
                    if p.name in locked or p.name in protected
                    or _is_preserved_path(p)]
        excluded_bytes = sum(s for _, s in excluded)
        excluded_paths = {p for p, _s in excluded}
        file_actions = [(p, s) for p, s in files if p not in excluded_paths]
        reclaim, reason = _classify_segment_dir(
            d.name, metas.get(int(d.name)) if d.name.isdigit() else None,
            active=bool(d.name.isdigit()) and int(d.name) in active,
            locked_hit=bool(locked_names),
            protected_hit=any(p.name in protected for p, _ in files),
            newest_mtime=newest, cutoff=cutoff, now=now,
        )
        decisions.append({
            "dir": d.name,
            "path": str(d),
            "numeric": d.name.isdigit(),
            "video_id": int(d.name) if d.name.isdigit() else None,
            "status": (metas.get(int(d.name)) or {}).get("status") if d.name.isdigit() else None,
            "reclaim": reclaim,
            "reason": reason if not reclaim else None,
            "reclaim_reason": reason if reclaim else None,
            "files_total": len(files),
            "bytes_total": sum(s for _, s in files),
            "file_actions": file_actions if reclaim else [],
            "excluded_files": len(excluded),
            "excluded_bytes": excluded_bytes,
            "can_rmdir": bool(reclaim) and not excluded,
        })
    return decisions


def _remove_empty_dirs(root: Path) -> int:
    """Bottom-up rmdir of ``root`` and subdirs; returns dirs removed.

    Non-empty directories (locked / preserved remnants) fail harmlessly and
    stay on disk for the next sweep — never forced, never silent about errors.
    """
    removed = 0
    if not root.is_dir():
        return 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        try:
            os.rmdir(dirpath)
            removed += 1
        except OSError:
            pass  # expected: non-empty / concurrently refilled
    return removed


def _purge_video_segments_dir(video_id: int, report: dict, protected: set[str],
                              locked: set[str], dry_run: bool) -> None:
    """Delete ``output/videos/segments/<video_id>`` for an uploaded video.

    Idempotent (missing dir → no-op) and lock-safe: locked / preserved /
    pending-referenced file names are never touched; the directory tree is
    removed only when nothing was excluded (safety net: leftover empty dirs
    are cleaned by the daily sweep).
    """
    seg_dir = segments_root() / str(video_id)
    if not seg_dir.is_dir():
        return
    files, _newest = _segment_dir_files(seg_dir)
    excluded = 0
    for p, _size in files:
        if p.name in locked:
            report["skipped_locked"].append(str(p))
            excluded += 1
            continue
        if p.name in protected or _is_preserved_path(p):
            report["skipped_protected"].append(str(p))
            excluded += 1
            continue
        report["freed_bytes"] += _unlink(p, report, "segments", dry_run, locked)
    if not dry_run and excluded == 0:
        report["dirs_removed"] = report.get("dirs_removed", 0) + \
            _remove_empty_dirs(seg_dir)


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

    # ── Render segments dir (leak fix oct 2026) ──────────────────
    # 0-day policy AT THE SOURCE: post-upload purge must also remove
    # ``output/videos/segments/<video_id>`` (scene checkpoints + dist
    # staging). The daily sweep keeps a safety net for dirs left by
    # crashed renders, but the upload path is the primary cleaner.
    _purge_video_segments_dir(video_id, report, protected, locked, dry_run)

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
            f"SELECT file_path FROM {table} WHERE {fk_col} = ? AND purged_at IS NULL",
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
        f"UPDATE {table} SET purged_at=datetime('now') "
        f"WHERE {fk_col}=? AND purged_at IS NULL",
        (entity_id,), report, dry_run, table,
    )


# ──────────────────────────────────────────────────────────────────
# Stale-pending reconciliation report (READ-ONLY — never deletes)
# ──────────────────────────────────────────────────────────────────

def _side_of(kind: str) -> str:
    return "short" if kind in ("short", "short_asset") else "video"


def _stale_pending_candidates(db, now: Optional[float] = None) -> dict:
    """Non-uploaded rows with no realistic upload path left (report-only).

    A video/short is a candidate when it is not uploaded, has no
    running/queued job, and:
      * long-form: terminal state (discarded/cancelled/validation_failed) any
        age, OR error/failed/retrying past the 48 h reassembly window, OR
        untouched for > ``STALE_PENDING_DAYS`` days;
      * short: cancelled/error/failed any age (shorts are regenerated as new
        rows — a failed row never re-uploads), OR untouched > 45 days.

    Returns ``{'video': {id: meta}, 'short': {id: meta}}``. Missing tables
    degrade to empty (never crash the sweep).
    """
    now = time.time() if now is None else now
    out = {"video": {}, "short": {}}
    conn = db._connect()

    def _age_days(meta):
        ts = _parse_db_ts(meta.get("updated_at")) or _parse_db_ts(meta.get("created_at"))
        return round((now - ts) / 86400.0, 1) if ts else None

    dead_v = ",".join("?" * len(TERMINAL_DEAD_VIDEO_STATUSES))
    retry_v = ",".join("?" * len(DEAD_RETRY_VIDEO_STATUSES))
    try:
        for row in conn.execute(
            f"""SELECT v.id, v.status, v.created_at, v.updated_at FROM videos v
                WHERE (v.yt_video_id IS NULL OR v.yt_video_id = '')
                  AND NOT EXISTS (SELECT 1 FROM generation_jobs gj
                                  WHERE gj.video_id = v.id
                                    AND gj.status IN ('running','queued'))
                  AND ( v.status IN ({dead_v})
                     OR (v.status IN ({retry_v})
                         AND COALESCE(v.updated_at, v.created_at) < datetime('now', '-{int(ERROR_LOCK_WINDOW_HOURS)} hours'))
                     OR COALESCE(v.updated_at, v.created_at) < datetime('now', '-{int(STALE_PENDING_DAYS)} days'))""",
            tuple(sorted(TERMINAL_DEAD_VIDEO_STATUSES))
            + tuple(sorted(DEAD_RETRY_VIDEO_STATUSES)),
        ):
            meta = dict(row)
            meta["age_days"] = _age_days(meta)
            out["video"][meta["id"]] = meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("media_retention: stale-pending videos query failed: %s", exc)

    dead_s = ",".join("?" * len(TERMINAL_DEAD_SHORT_STATUSES))
    try:
        for row in conn.execute(
            f"""SELECT s.id, s.status, s.created_at, s.updated_at FROM shorts s
                WHERE (s.youtube_id IS NULL OR s.youtube_id = '')
                  AND NOT EXISTS (SELECT 1 FROM generation_jobs gj
                                  WHERE gj.result_short_id = s.id
                                    AND gj.status IN ('running','queued'))
                  AND ( s.status IN ({dead_s})
                     OR COALESCE(s.updated_at, s.created_at) < datetime('now', '-{int(STALE_PENDING_DAYS)} days'))""",
            tuple(sorted(TERMINAL_DEAD_SHORT_STATUSES)),
        ):
            meta = dict(row)
            meta["age_days"] = _age_days(meta)
            out["short"][meta["id"]] = meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("media_retention: stale-pending shorts query failed: %s", exc)

    return out


def _stale_pending_report(db, refs: dict, protected_files: dict[str, tuple[int, str]],
                          seg_decisions: list[dict]) -> dict:
    """Summarise dead/stuck pending rows and the material they protect.

    ``protected_files`` maps ``str(path) → (size, basename)`` for every file
    the sweep classified as ``referenced_by_pending``. ``seg_decisions`` are
    the segments-sweep decisions (for per-video dir sizes).

    REPORT ONLY: nothing here deletes or mutates rows. The operator
    reconciles (archive/discard DB rows), after which the daily sweep frees
    the material automatically.
    """
    cand = _stale_pending_candidates(db)
    prot_rows = refs.get("protected_rows", {})

    entities: dict[tuple[str, int], dict] = {}

    def _entity(side: str, eid: int):
        meta = cand.get(side, {}).get(eid)
        key = (side, eid)
        e = entities.get(key)
        if e is None:
            e = {
                "kind": side, "id": eid,
                "status": (meta or {}).get("status"),
                "age_days": (meta or {}).get("age_days"),
                "files": 0, "bytes": 0,
                "segments_dir": None, "segments_files": 0, "segments_bytes": 0,
            }
            entities[key] = e
        return e

    only_stale_files = 0
    only_stale_bytes = 0
    for path, (size, bn) in protected_files.items():
        rows = prot_rows.get(bn) or []
        if not rows:
            continue
        if not all(
            r["id"] is not None and r["id"] in cand[_side_of(r["kind"])]
            for r in rows
        ):
            continue  # some protector is still genuinely live → not reconcilable
        only_stale_files += 1
        only_stale_bytes += size
        for r in {(_side_of(x["kind"]), x["id"]) for x in rows}:
            e = _entity(*r)
            e["files"] += 1
            e["bytes"] += size

    stale_segments_bytes = 0
    for dec in seg_decisions:
        vid = dec.get("video_id")
        if vid is None or dec["reclaim"] or vid not in cand["video"]:
            continue
        # dir still preserved by a stale row (terminal dead / old error /
        # ancient pending): reconciling the row makes it reclaimable.
        e = _entity("video", vid)
        e["segments_dir"] = dec["path"]
        e["segments_files"] = dec["files_total"]
        e["segments_bytes"] = dec["bytes_total"]
        stale_segments_bytes += dec["bytes_total"]

    detail = sorted(entities.values(),
                    key=lambda e: -(e["bytes"] + e["segments_bytes"]))
    # candidates protecting nothing on disk (rows to clean for hygiene only)
    for side in ("video", "short"):
        for eid, meta in cand[side].items():
            if (side, eid) not in entities:
                detail.append({
                    "kind": side, "id": eid, "status": meta["status"],
                    "age_days": meta["age_days"],
                    "files": 0, "bytes": 0, "segments_dir": None,
                    "segments_files": 0, "segments_bytes": 0,
                })

    return {
        "note": ("Report only. Reconcile = operator marks/archives the DB rows "
                 "(scripts/discard_videos.py or status change); the next daily "
                 "sweep then reclaims the material. Nothing here deletes."),
        "entities": detail,
        "entities_total": len(detail),
        "files_only_stale_protected": only_stale_files,
        "bytes_only_stale_protected": only_stale_bytes,
        "segments_bytes_stale_rows": stale_segments_bytes,
        "reclaimable_if_reconciled_bytes": only_stale_bytes + stale_segments_bytes,
    }


# ──────────────────────────────────────────────────────────────────
# Orphan sweep (retroactive + periodic)
# ──────────────────────────────────────────────────────────────────

def _segments_cat_info(root: Path, decisions: list[dict]) -> dict:
    """Aggregate directory decisions into the manifest category shape.

    Includes the legacy per-file keys (``files``/``bytes``/``reclaim_files``/
    ``reclaim_bytes``/``reasons``) so older consumers keep working, plus the
    explicit segments observability keys.
    """
    files_total = sum(d["files_total"] for d in decisions)
    bytes_total = sum(d["bytes_total"] for d in decisions)
    reclaim_files = sum(len(d["file_actions"]) for d in decisions
                        if d["reclaim"])
    reclaim_bytes = sum(s for d in decisions
                        if d["reclaim"] for _p, s in d["file_actions"])
    reasons: dict[str, int] = {}
    reclaim_reasons: dict[str, int] = {}
    for d in decisions:
        if d["reclaim"]:
            reclaim_reasons[d["reclaim_reason"]] = \
                reclaim_reasons.get(d["reclaim_reason"], 0) + 1
        else:
            reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
    return {
        "root": str(root),
        # legacy keys
        "files": files_total,
        "bytes": bytes_total,
        "reclaim_files": reclaim_files,
        "reclaim_bytes": reclaim_bytes,
        "reasons": reasons,
        # segments observability (oct 2026)
        "dirs_total": len(decisions),
        "dirs_reclaim": sum(1 for d in decisions if d["reclaim"]),
        "files_total": files_total,
        "files_reclaim": reclaim_files,
        "bytes_total": bytes_total,
        "protected_bytes": bytes_total - reclaim_bytes,
        "reclaim_reasons": reclaim_reasons,
    }


def classify_reclaim(db, categories: Optional[Iterable[str]] = None,
                     min_age_hours: float = 24.0) -> dict:
    """Compute (without deleting) what the sweep would reclaim.

    A file is reclaimable when it is NOT referenced by any non-uploaded
    entity, is NOT locked, is older than ``min_age_hours`` and is not a
    preserved artifact (thumbnail / main SEO subtitle).

    Directory-granularity categories (:data:`DIR_SWEEP_CATEGORIES`, e.g.
    ``segments``) are classified per video-id directory by
    :func:`plan_segments_sweep` instead of per file.

    Adds a read-only ``stale_pending_entities`` section listing dead/stuck
    non-uploaded rows that keep material protected forever (see
    :func:`_stale_pending_report`). Pure read: never deletes, never writes.
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

    seg_decisions: list[dict] = []
    protected_files: dict[str, tuple[int, str]] = {}

    for cat in cats:
        spec = SWEEP_CATEGORIES.get(cat)
        if not spec:
            result["errors"].append({"error": f"unknown category {cat}"})
            continue
        root, recursive = spec

        if cat in DIR_SWEEP_CATEGORIES:
            decisions = plan_segments_sweep(db, refs, locked, cutoff)
            if cat == "segments":
                seg_decisions = decisions
            cat_info = _segments_cat_info(root, decisions)
            result["categories"][cat] = cat_info
            result["total_bytes"] += cat_info["bytes"]
            result["reclaim_bytes"] += cat_info["reclaim_bytes"]
            result["files_total"] += cat_info["files"]
            result["files_reclaim"] += cat_info["reclaim_files"]
            continue

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
                    if reason == "referenced_by_pending":
                        protected_files[str(path)] = (size, path.name)
        result["categories"][cat] = cat_info
        result["total_bytes"] += cat_info["bytes"]
        result["reclaim_bytes"] += cat_info["reclaim_bytes"]
        result["files_total"] += cat_info["files"]
        result["files_reclaim"] += cat_info["reclaim_files"]

    try:
        result["stale_pending_entities"] = _stale_pending_report(
            db, refs, protected_files, seg_decisions)
    except Exception as exc:  # noqa: BLE001 — report must never break the manifest
        logger.warning("media_retention: stale-pending report failed: %s", exc)
        result["errors"].append({"error": f"stale_pending: {exc}"})

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
        if cat in DIR_SWEEP_CATEGORIES:
            for dec in plan_segments_sweep(db, refs, locked, cutoff):
                if dec["reclaim"]:
                    for path, size in dec["file_actions"]:
                        out[str(path)] = size
            continue
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
    Directory categories (``segments``) additionally remove the reclaimed
    directories once their files are gone (``dirs_removed`` in the report).
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

        if cat in DIR_SWEEP_CATEGORIES:
            cat_freed = 0
            cat_files = 0
            dirs_removed = 0
            for dec in plan_segments_sweep(db, refs, locked, cutoff):
                if not dec["reclaim"]:
                    continue
                for path, _size in dec["file_actions"]:
                    freed = _unlink(path, report, cat, dry_run, locked)
                    if freed:
                        cat_freed += freed
                        cat_files += 1
                if dec["can_rmdir"] and not dry_run:
                    dirs_removed += _remove_empty_dirs(Path(dec["path"]))
            report["categories"][cat] = {"freed_bytes": cat_freed,
                                         "files": cat_files,
                                         "dirs_removed": dirs_removed}
            report["freed_bytes"] += cat_freed
            continue

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
