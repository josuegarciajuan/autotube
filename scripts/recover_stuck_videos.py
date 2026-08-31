#!/usr/bin/env python3
"""Recover explicitly selected videos left in ``ready`` after reassembly.

Generates thumbnails, populates metadata, and triggers upload to YouTube.
"""
import json
import logging
import sqlite3
import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from scripts.runtime_context import add_channel_selector_arguments, resolve_channels, SelectorError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recovery")

DB_PATH = Path(settings.DATABASE_PATH)
PROJECT_ROOT = settings.PROJECT_ROOT

def get_video(db, video_id):
    return db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()


def get_script(db, script_id):
    return db.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()


def generate_thumbnail(video_path, canal, video_id):
    """Generate a thumbnail from the brightest (non-black) frame of the video."""
    from pipeline.frame_thumb import extract_thumbnail_frame

    out_dir = PROJECT_ROOT / "output" / "thumbnails" / canal
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = out_dir / f"recover_{video_id}.jpg"

    log.info("Generating thumbnail for %s -> %s", video_path, thumb_path)
    result = extract_thumbnail_frame(video_path, thumb_path, label=f"recover_{video_id}")
    if result and result.exists():
        log.info("Thumbnail generated: %s (%d bytes)", result, result.stat().st_size)
        return str(result)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_channel_selector_arguments(parser)
    parser.add_argument("--video-id", action="append", type=int, required=True,
                        help="ID de vídeo a recuperar (repetible; obligatorio)")
    parser.add_argument("--dry-run", action="store_true", help="mostrar alcance sin modificar DB ni archivos")
    args = parser.parse_args()
    try:
        contexts = resolve_channels(
            db_path=DB_PATH, channel_id=args.channel_id, slug=args.slug,
            project=args.project, all_channels=args.all_channels, yes=args.yes,
        )
    except SelectorError as exc:
        parser.error(str(exc))
    selected_slugs = {context.slug for context in contexts}
    candidate_ids = set(args.video_id)
    db_conn = sqlite3.connect(str(DB_PATH))
    db_conn.row_factory = sqlite3.Row

    for video_id in sorted(candidate_ids):
        log.info("=== Processing video %d ===", video_id)
        video = get_video(db_conn, video_id)
        if not video:
            log.error("Video %d not found!", video_id)
            continue

        video = dict(video)
        canal = video.get("canal", "")
        if canal not in selected_slugs:
            continue
        video_path = video.get("video_path", "")

        if not video_path or not Path(video_path).exists():
            log.error("Video file not found: %s", video_path)
            continue

        # Get checkpoint data to find script_id
        cp = {}
        try:
            cp = json.loads(video.get("checkpoint_data", "{}"))
        except json.JSONDecodeError:
            pass

        script_id = None
        if isinstance(cp.get("script"), dict):
            script_id = cp["script"].get("id")
        if not script_id:
            script_id = video.get("script_id")

        if not script_id:
            log.error("No script_id found for video %d", video_id)
            continue

        script = get_script(db_conn, script_id)
        if not script:
            log.error("Script %d not found for video %d", script_id, video_id)
            continue

        script = dict(script)

        # Extract metadata
        try:
            titulo_options = json.loads(script.get("titulo_options", "[]"))
        except json.JSONDecodeError:
            titulo_options = []
        titulo = titulo_options[0] if titulo_options else "Video"

        # Use titulo_selected if available
        titulo_selected = script.get("titulo_selected")
        if titulo_selected:
            titulo = titulo_selected

        try:
            keywords = json.loads(script.get("keywords_json", "[]"))
        except json.JSONDecodeError:
            keywords = []

        guion = script.get("guion", "")
        description = guion[:500] if guion else f"Video sobre: {titulo}"

        log.info("Title: %s", titulo)
        log.info("Keywords: %s", keywords)

        if args.dry_run:
            log.info("DRY RUN: video %d listo para recuperar en %s", video_id, canal)
            continue

        # Generate thumbnail
        thumb_path = generate_thumbnail(video_path, canal, video_id)

        # Update video in database
        channel_id = video.get("channel_id")
        if not channel_id:
            channel_id = next(c.id for c in contexts if c.slug == canal)
        db_conn.execute(
            """UPDATE videos SET
               titulo_final = ?,
               description = ?,
               tags_json = ?,
               thumbnail_path = ?,
               channel_id = ?
               WHERE id = ?""",
            (
                titulo,
                description,
                json.dumps(keywords, ensure_ascii=False),
                thumb_path or "",
                channel_id,
                video_id,
            )
        )
        db_conn.commit()
        log.info("Video %d metadata updated in DB", video_id)

        # Create upload job
        db_conn.execute(
            """INSERT INTO generation_jobs
               (channel_id, video_id, action, status, created_at)
               VALUES (?, ?, 'upload', 'queued', datetime('now', 'localtime'))""",
            (channel_id, video_id)
        )
        db_conn.commit()
        job_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.info("Upload job %d created for video %d", job_id, video_id)

    db_conn.close()
    log.info("=== Recovery complete ===")
    log.info("The upload jobs are queued. They will be picked up by the API scheduler.")
    log.info("To trigger immediately, restart the API or wait for the next tick.")


if __name__ == "__main__":
    main()
