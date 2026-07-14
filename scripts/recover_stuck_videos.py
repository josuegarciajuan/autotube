#!/usr/bin/env python3
"""Recover stuck videos 304 and 305 that were left in 'ready' state after reassembly.

Generates thumbnails, populates metadata, and triggers upload to YouTube.
"""
import json
import logging
import sqlite3
import subprocess
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("recovery")

DB_PATH = Path("/root/autotube/autotube.db")
PROJECT_ROOT = Path("/root/autotube")

STUCK_VIDEOS = [304, 305]

CHANNEL_MAP = {"canal2": 3, "canal3": 4}


def get_video(db, video_id):
    return db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()


def get_script(db, script_id):
    return db.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()


def generate_thumbnail(video_path, canal, video_id):
    """Generate a thumbnail from a frame of the video."""
    out_dir = PROJECT_ROOT / "output" / "thumbnails" / canal
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = out_dir / f"recover_{video_id}.jpg"

    log.info("Generating thumbnail for %s -> %s", video_path, thumb_path)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-ss", "00:00:15", "-vframes", "1",
         "-q:v", "2", str(thumb_path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        log.error("ffmpeg failed: %s", result.stderr[:200])
        return None
    if thumb_path.exists():
        log.info("Thumbnail generated: %s (%d bytes)", thumb_path, thumb_path.stat().st_size)
        return str(thumb_path)
    return None


def main():
    db_conn = sqlite3.connect(str(DB_PATH))
    db_conn.row_factory = sqlite3.Row

    for video_id in STUCK_VIDEOS:
        log.info("=== Processing video %d ===", video_id)
        video = get_video(db_conn, video_id)
        if not video:
            log.error("Video %d not found!", video_id)
            continue

        video = dict(video)
        canal = video.get("canal", "canal2")
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

        # Generate thumbnail
        thumb_path = generate_thumbnail(video_path, canal, video_id)

        # Update video in database
        channel_id = video.get("channel_id") or CHANNEL_MAP.get(canal, 3)
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
