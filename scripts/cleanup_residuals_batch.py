#!/usr/bin/env python3
"""Batch cleanup: delete residual files from already-uploaded videos.

Deletes for every uploaded/published video:
  - MP3 narration + CTA audio + derived CTA files (timestamps, subtitles)
  - All scene assets (images, video clips, AI scenes) referenced in video_scenes
  - MP4 video files still lingering on disk

Preserves:
  - Main narration SRT subtitles + timestamps JSON (SEO value, analysis)
  - Thumbnails (panel display)

Usage:
  python3 scripts/cleanup_residuals_batch.py              # Dry-run (preview only)
  python3 scripts/cleanup_residuals_batch.py --execute    # Execute deletion
  python3 scripts/cleanup_residuals_batch.py --execute --populate-history  # Also fill history table
  python3 scripts/cleanup_residuals_batch.py --canal canal2   # Single channel only
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Ensure autotube root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db_extended import ExtendedDatabase
from pipeline.cleanup_utils import (
    cleanup_video_residuals,
    _extract_paths_from_image_path,
)


def format_size(bytes_val: int) -> str:
    """Human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} TB"


def count_existing_videos(db) -> int:
    """Count videos that have been uploaded to YouTube."""
    videos = db._connect().execute(
        """SELECT COUNT(*) as cnt FROM videos
           WHERE yt_video_id IS NOT NULL
             AND status IN ('uploaded', 'uploaded_private', 'published')"""
    ).fetchone()
    return videos["cnt"] if videos else 0


def main():
    parser = argparse.ArgumentParser(
        description="Batch cleanup of residual files from uploaded videos"
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Execute deletion (default: dry-run preview only)",
    )
    parser.add_argument(
        "--populate-history", action="store_true",
        help="Also populate video_asset_history from video_scenes data",
    )
    parser.add_argument(
        "--canal", type=str, default=None,
        help="Limit to a single channel slug (e.g., canal2)",
    )
    parser.add_argument(
        "--skip-mp3", action="store_true",
        help="Skip MP3/CTA audio deletion",
    )
    args = parser.parse_args()

    dry_run = not args.execute
    db = ExtendedDatabase()

    if dry_run:
        print("=" * 60)
        print("  DRY RUN — no files will be deleted")
        print("  Add --execute to actually delete files")
        print("=" * 60)

    # ── Find uploaded videos ─────────────────────────────────
    conn = db._connect()
    query = """SELECT v.id, v.yt_video_id, v.audio_path, v.video_path,
                      v.titulo_final, v.status, v.checkpoint_data,
                      c.slug as canal_slug
               FROM videos v
               LEFT JOIN channels c ON v.channel_id = c.id
               WHERE v.yt_video_id IS NOT NULL
                 AND v.status IN ('uploaded', 'uploaded_private', 'published')"""
    params = ()
    if args.canal:
        query += " AND c.slug = ?"
        params = (args.canal,)
    query += " ORDER BY v.id"

    rows = conn.execute(query, params).fetchall()

    if not rows:
        print("No uploaded videos found.")
        return

    print(f"\nFound {len(rows)} uploaded videos.\n")

    total_freed = 0
    total_files_deleted = 0
    total_history_inserted = 0
    videos_processed = 0
    errors = 0

    for row in rows:
        video_id = row["id"]
        canal = row["canal_slug"] or "?"
        yt_id = row["yt_video_id"] or "?"
        title = (row["titulo_final"] or "?")[:50]

        try:
            if not dry_run or not args.skip_mp3:
                # Check audio_path from DB
                audio_path = row["audio_path"] or ""

                # Extract checkpoint CTA
                cp_raw = row["checkpoint_data"] or "{}"
                cta_path = ""
                try:
                    cp = json.loads(cp_raw) if isinstance(cp_raw, str) else cp_raw
                    cta_path = cp.get("tts", {}).get("cta_audio_path", "")
                except (json.JSONDecodeError, TypeError):
                    pass

                audio_data = {"audio_path": audio_path, "cta_audio_path": cta_path}

            if dry_run:
                # Estimate
                freed = _estimate_freed(db, video_id, row)
                total_freed += freed
                print(f"  [#{video_id}] {canal} | {yt_id[:11]}... | {title}")
                if freed > 0:
                    print(f"          ~{format_size(freed)} to free")
                else:
                    print(f"          (nothing to clean)")
            else:
                freed = cleanup_video_residuals(
                    db, video_id,
                    audio_data=audio_data if not args.skip_mp3 else None,
                )
                total_freed += freed
                videos_processed += 1
                print(f"  [#{video_id}] {canal} | {yt_id[:11]}... | {title}")
                print(f"          Freed {format_size(freed)}")

            # ── Populate history table ──────────────────────────
            if args.populate_history and not dry_run:
                inserted = _populate_history_from_scenes(db, video_id)
                total_history_inserted += inserted

        except Exception as exc:
            errors += 1
            print(f"  [#{video_id}] ERROR: {exc}")

    # ── Summary ──────────────────────────────────────────────
    print()
    print("=" * 60)
    if dry_run:
        print(f"  DRY RUN SUMMARY")
        print(f"  Videos with residuals: {videos_processed} of {len(rows)}")
        print(f"  Estimated space to free: {format_size(total_freed)}")
    else:
        print(f"  CLEANUP COMPLETE")
        print(f"  Videos processed: {videos_processed}")
        print(f"  Errors: {errors}")
        print(f"  Total space freed: {format_size(total_freed)}")
        if args.populate_history:
            print(f"  History records inserted: {total_history_inserted}")
    print("=" * 60)


def _estimate_freed(db, video_id: int, row) -> int:
    """Estimate space that would be freed (for dry-run reporting)."""
    freed = 0
    project_root = Path(__file__).resolve().parent.parent

    # MP3 audio
    ap = row["audio_path"] or ""
    if ap:
        p = project_root / ap
        if p.exists():
            freed += p.stat().st_size

    # CTA audio from checkpoint
    cp_raw = row["checkpoint_data"] or "{}"
    try:
        cp = json.loads(cp_raw) if isinstance(cp_raw, str) else cp_raw
        cta = cp.get("tts", {}).get("cta_audio_path", "")
        if cta:
            p = project_root / cta
            if p.exists():
                freed += p.stat().st_size
    except Exception:
        pass

    # MP4 if still lingering
    vp = row["video_path"] or ""
    if vp:
        p = project_root / vp
        if p.exists():
            freed += p.stat().st_size

    # Scene assets
    try:
        scenes = db.get_scenes(video_id)
        for scene in scenes:
            image_path = scene.get("image_path", "")
            paths = _extract_paths_from_image_path(image_path)
            for fp in paths:
                p = project_root / fp
                if p.exists():
                    freed += p.stat().st_size
    except Exception:
        pass

    return freed


def _populate_history_from_scenes(db, video_id: int) -> int:
    """Populate video_asset_history from video_scenes data.

    Parses image_path to extract source and relative path, then
    inserts records that were missed by the new cross-video dedup system.
    """
    inserted = 0
    try:
        scenes = db.get_scenes(video_id)
        project_root = Path(__file__).resolve().parent.parent

        for scene in scenes:
            image_path = scene.get("image_path", "")
            paths = _extract_paths_from_image_path(image_path)
            for fp in paths:
                # Parse source from filename
                # e.g., output/images/pixabay_photo_123456.jpg → pixabay_photo
                # or output/video_clips/pexels_abc123.mp4 → pexels_video
                p = Path(fp)
                stem = p.stem
                # Determine source type from directory and filename pattern
                if "video_clips" in fp:
                    # Filename: pexels_abc123.mp4 → source = pexels_video
                    parts = stem.split("_", 1)
                    source = f"{parts[0]}_video" if parts else "unknown_video"
                elif "ai_scenes" in fp:
                    source = "pollo_ai"
                elif "images" in fp:
                    # Filename: pixabay_photo_123456.jpg
                    # or unsplash_123456.jpg
                    if stem.startswith("pixabay_photo_"):
                        source = "pixabay_photo"
                    elif stem.startswith("pexels_photo_"):
                        source = "pexels_photo"
                    elif stem.startswith("unsplash_"):
                        source = "unsplash"
                    else:
                        source = "unknown_image"
                else:
                    source = "unknown"

                # Normalize path to relative
                if p.is_absolute():
                    try:
                        rel = p.relative_to(project_root).as_posix()
                    except ValueError:
                        rel = str(p)
                else:
                    rel = str(p)

                try:
                    db.insert_asset_history(
                        video_id=video_id,
                        file_path=rel,
                        source=source,
                        asset_url="",
                    )
                    inserted += 1
                except Exception:
                    pass
    except Exception:
        pass

    return inserted


if __name__ == "__main__":
    main()
