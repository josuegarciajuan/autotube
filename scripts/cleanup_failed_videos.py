#!/usr/bin/env python3
"""
Delete all failed (status='error') videos from the database and clean up
their associated temporary files (MP4, MP3, SRT, thumbnails, CTA audio).

Usage:
    python3 scripts/cleanup_failed_videos.py           # interactive (asks confirmation)
    python3 scripts/cleanup_failed_videos.py --yes     # skip confirmation
    python3 scripts/cleanup_failed_videos.py --dry-run # preview only, no changes
    python3 scripts/cleanup_failed_videos.py --deep    # also remove orphaned scene files
"""

import sqlite3
import json
import logging
import sys
import os
import argparse
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("cleanup_failed")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "autotube.db"


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_path(stored_path: str | None) -> Path | None:
    """Resolve a stored path (relative or absolute) to an absolute Path."""
    if not stored_path:
        return None
    p = Path(stored_path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _is_protected_thumbnail(fp: Path) -> bool:
    """Return True if the file is under output/thumbnails/{slug}/ (YouTube CDN data).
    
    Per AGENTS.md, output/thumbnails/ subdirectories (slug-based) must NEVER be deleted.
    Only root-level thumbnails (e.g. output/thumbnails/thumb_foo.jpg) are safe to delete.
    """
    try:
        parts = fp.relative_to(PROJECT_ROOT).parts
    except ValueError:
        return False
    # Protected: output/thumbnails/{slug}/...  (depth >= 3 and parts[0]=='output', parts[1]=='thumbnails')
    # Safe:     output/thumbnails/thumb_*.jpg   (depth == 2)
    if len(parts) >= 3 and parts[0] == "output" and parts[1] == "thumbnails":
        return True
    return False


def _collect_files(video: sqlite3.Row) -> list[tuple[Path, str]]:
    """Collect all file paths associated with a failed video. Returns (path, label)."""
    files: list[tuple[Path, str]] = []

    # Main video MP4
    p = _resolve_path(video["video_path"])
    if p and p.exists():
        files.append((p, "mp4"))

    # Audio MP3 + derived files (timestamps, subtitles)
    ap = _resolve_path(video["audio_path"])
    if ap:
        if ap.exists():
            files.append((ap, "mp3"))
        stem_dir = ap.parent
        stem = ap.stem
        for suffix in ["_timestamps.json", "_subtitles.srt"]:
            derived = stem_dir / f"{stem}{suffix}"
            if derived.exists():
                files.append((derived, suffix.lstrip("_").replace(".", " ")))

    # Thumbnail — protect YouTube CDN thumbnails
    tp = _resolve_path(video["thumbnail_path"])
    if tp and tp.exists():
        if _is_protected_thumbnail(tp):
            logger.warning("  [SKIP] Protected YouTube CDN thumbnail: %s", tp)
        else:
            files.append((tp, "thumb"))

    # CTA audio from checkpoint_data (optional intro/outro audio)
    try:
        chk = json.loads(video["checkpoint_data"]) if video["checkpoint_data"] else {}
    except (json.JSONDecodeError, TypeError):
        chk = {}
    cta_path = chk.get("tts", {}).get("cta_audio_path")
    if cta_path:
        cp = _resolve_path(cta_path)
        if cp:
            if cp.exists():
                files.append((cp, "cta mp3"))
            stem_dir = cp.parent
            stem = cp.stem
            for suffix in ["_timestamps.json", "_subtitles.srt"]:
                derived = stem_dir / f"{stem}{suffix}"
                if derived.exists():
                    files.append((derived, f"cta {suffix.lstrip('_').replace('.', ' ')}"))

    return files


def show_before():
    """Display current DB state and preview what will be deleted."""
    conn = get_conn()
    print("\n=== BEFORE ===")
    videos_status = conn.execute(
        "SELECT status, COUNT(*) c FROM videos GROUP BY status ORDER BY c DESC"
    ).fetchall()
    print("  Videos by status:")
    for r in videos_status:
        marker = " ← TARGET" if r["status"] == "error" else ""
        print(f"    {r['status']:20} {r['c']}{marker}")

    error_videos = conn.execute(
        "SELECT id, titulo_final, canal, video_path, audio_path, thumbnail_path, checkpoint_data, "
        "progress_phase FROM videos WHERE status = 'error' ORDER BY id"
    ).fetchall()
    print(f"\n  Error videos to delete: {len(error_videos)}")

    total_files = 0
    total_size = 0
    by_phase: dict[str, int] = {}
    for v in error_videos:
        files = _collect_files(v)
        total_files += len(files)
        for fp, _label in files:
            total_size += fp.stat().st_size
        phase = v["progress_phase"] or "unknown"
        by_phase[phase] = by_phase.get(phase, 0) + 1
        if len(error_videos) <= 30:
            titulo = v["titulo_final"] or "(no title)"
            canal = v["canal"] or ""
            print(f"    [{v['id']}] {canal:8} {titulo[:65]}  ({len(files)} files)")

    if len(error_videos) > 30:
        print(f"    ... and {len(error_videos) - 30} more")

    if by_phase:
        print(f"\n  By failure phase:")
        for phase, count in sorted(by_phase.items()):
            print(f"    {phase:20} {count}")

    print(f"\n  Total files to delete: {total_files}")
    print(f"  Total recoverable disk space: {_format_size(total_size)}")
    print()
    conn.close()
    return error_videos


def delete_failed_videos(dry_run: bool = False, deep: bool = False):
    """Delete all error-status videos: files on disk + DB records."""
    conn = get_conn()

    error_videos = conn.execute(
        "SELECT id, titulo_final, canal, video_path, audio_path, thumbnail_path, checkpoint_data, "
        "progress_phase FROM videos WHERE status = 'error' ORDER BY id"
    ).fetchall()

    if not error_videos:
        print("No failed videos to delete.")
        conn.close()
        return

    logger.info("Processing %d failed videos%s", len(error_videos), " (DRY RUN)" if dry_run else "")

    if not dry_run:
        # Start a single transaction for all DB deletions
        conn.execute("BEGIN")

    total_files_deleted = 0
    total_bytes_freed = 0

    for v in error_videos:
        vid = v["id"]
        files = _collect_files(v)

        for fp, label in files:
            try:
                fsize = fp.stat().st_size
                if not dry_run:
                    fp.unlink(missing_ok=True)
                logger.info("  [%s] (%s) %s  %s", "DRY" if dry_run else "OK ", label, _format_size(fsize), fp)
                total_files_deleted += 1
                total_bytes_freed += fsize
            except OSError as e:
                logger.warning("  [ERR] %s: %s", fp, e)

        if not dry_run:
            conn.execute("DELETE FROM video_scenes WHERE video_id = ?", (vid,))
            conn.execute("DELETE FROM videos WHERE id = ?", (vid,))

    if not dry_run:
        conn.execute("COMMIT")
        logger.info("DB records deleted for %d videos", len(error_videos))

    logger.info("Deleted %d videos, %d files, freed %s%s",
                len(error_videos), total_files_deleted, _format_size(total_bytes_freed),
                " (DRY RUN)" if dry_run else "")

    if deep and not dry_run:
        _clean_orphaned_scene_files(conn)

    conn.close()


def _clean_orphaned_scene_files(conn):
    """Remove scene files no longer referenced by any video_scene row.

    This is a separate step that runs AFTER the failed video deletion.
    It scans output/images/, output/video_clips/, output/ai_scenes/ and
    removes files that have zero references in the video_scenes table.
    Files shared across multiple videos are preserved.
    """
    logger.info("\n--- Deep clean: scanning for orphaned scene files ---")

    refs: set[str] = set()
    rows = conn.execute(
        "SELECT DISTINCT image_path FROM video_scenes WHERE image_path IS NOT NULL AND image_path != ''"
    ).fetchall()
    for r in rows:
        val = str(r["image_path"]).strip()
        if val:
            refs.add(val)

    logger.info("  %d distinct scene files still referenced", len(refs))

    scene_dirs = [
        PROJECT_ROOT / "output" / "images",
        PROJECT_ROOT / "output" / "video_clips",
        PROJECT_ROOT / "output" / "ai_scenes",
    ]

    deleted = 0
    freed = 0
    for sdir in scene_dirs:
        if not sdir.is_dir():
            logger.info("  [SKIP] Directory not found: %s", sdir)
            continue
        for fpath in sdir.iterdir():
            if not fpath.is_file():
                continue
            try:
                rel = fpath.relative_to(PROJECT_ROOT).as_posix()
            except ValueError:
                rel = str(fpath)
            if rel not in refs:
                try:
                    fsize = fpath.stat().st_size
                    fpath.unlink()
                    deleted += 1
                    freed += fsize
                except OSError as e:
                    logger.warning("  [ERR] %s: %s", fpath, e)

    logger.info("Deep clean: removed %d orphaned scene files, freed %s", deleted, _format_size(freed))


def show_after():
    """Display DB state and disk file counts after cleanup."""
    conn = get_conn()
    print("\n=== AFTER ===")
    videos_status = conn.execute(
        "SELECT status, COUNT(*) c FROM videos GROUP BY status ORDER BY c DESC"
    ).fetchall()
    print("  Videos by status:")
    for r in videos_status:
        print(f"    {r['status']:20} {r['c']}")

    for label, subdir in [
        ("images", "output/images"),
        ("video_clips", "output/video_clips"),
        ("ai_scenes", "output/ai_scenes"),
        ("audio", "output/audio"),
    ]:
        d = PROJECT_ROOT / subdir
        if d.exists():
            count = sum(1 for _ in d.iterdir() if _.is_file())
            print(f"  {label}: {count} files on disk")
    print()
    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Delete all failed (status='error') videos and their temp files"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--deep", action="store_true",
                        help="Also remove orphaned scene files (images/video_clips/ai_scenes with no DB references)")
    args = parser.parse_args()

    error_videos = show_before()

    if not error_videos:
        return

    if not args.dry_run and not args.yes:
        ans = input(f"\nDelete {len(error_videos)} failed videos and their files? Type 'yes' to confirm: ")
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return

    delete_failed_videos(dry_run=args.dry_run, deep=args.deep)

    show_after()


if __name__ == "__main__":
    main()
