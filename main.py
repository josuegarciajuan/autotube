#!/usr/bin/env python3
"""Autotube — Automated YouTube Channel Pipeline.

Usage:
    # Full pipeline (scrape → script → TTS → images → video → upload)
    python main.py run --canal canal2

    # Full pipeline without YouTube upload (save video locally)
    python main.py run --canal canal2 --skip-upload

    # Scheduled mode (continuous operation)
    python main.py serve --canal canal2

    # Scrape only (populate database with fresh content)
    python main.py scrape --canal canal2

    # Generate scripts only (from existing scraped content)
    python main.py generate --canal canal2

    # Upload a specific video file
    python main.py upload --canal canal2 --video /path/to/video.mp4 --title "My Title"

    # Database stats
    python main.py stats --canal canal2
"""

import argparse
import sys
import logging
from pathlib import Path

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import ACTIVE_CHANNELS, LOG_LEVEL, LOGS_DIR, LOG_FORMAT
from database.db import Database, init_db
from database.db_extended import migrate_v2

logger = logging.getLogger("autotube")


def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOGS_DIR / "autotube.log", encoding="utf-8"),
        ],
    )
    for lib in ["urllib3", "googleapiclient", "google.auth", "apscheduler", "PIL"]:
        logging.getLogger(lib).setLevel(logging.WARNING)


def cmd_run(args):
    """Run the full pipeline once."""
    from orchestrator import PipelineOrchestrator
    from database.db_extended import ExtendedDatabase

    canal = args.canal or ACTIVE_CHANNELS[0]
    
    # ── Guard: don't run if API has an active generation ──
    db = ExtendedDatabase()
    active = db.get_active_job()
    if active:
        logger.error("❌ Active job #%d is running in the API — aborting CLI run to avoid conflict", active["id"])
        return 1
    
    # ── Memory check ──
    import os
    try:
        avail_bytes = os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
        avail_gb = avail_bytes / (1024 ** 3)
        if avail_gb < 4.0:
            logger.warning("⚠️  Only %.1f GB free — rendering may fail with OOM", avail_gb)
    except Exception:
        pass
    
    orch = PipelineOrchestrator(canal=canal)

    # Quick content check before starting
    unused = orch.db.get_unused_count(canal)
    logger.info(f"Channel: {canal} | Unused content: {unused} | "
                f"Videos today: {orch.db.get_videos_today(canal)}")

    success = orch.run_full_pipeline(skip_upload=args.skip_upload)
    return 0 if success else 1


def cmd_serve(args):
    """Run the pipeline in scheduled (daemon) mode."""
    from orchestrator import PipelineOrchestrator

    canal = args.canal or ACTIVE_CHANNELS[0]
    orch = PipelineOrchestrator(canal=canal)

    logger.info(f"Channel: {canal}")
    logger.info(f"Videos per day: {orch.get_videos_per_day()}")
    logger.info(f"Unused content in DB: {orch.db.get_unused_count(canal)}")
    logger.info(f"Videos uploaded today: {orch.db.get_videos_today(canal)}")

    orch.start_scheduler()

    # Keep alive
    import signal
    stop = signal.Event()
    signal.signal(signal.SIGINT, lambda s, f: stop.set())
    signal.signal(signal.SIGTERM, lambda s, f: stop.set())
    logger.info(f"Scheduler running. Press Ctrl+C to stop.")
    stop.wait()
    logger.info("Shutting down...")
    return 0


def cmd_scrape(args):
    """Scrape content only and save to database."""
    from orchestrator import PipelineOrchestrator

    canal = args.canal or ACTIVE_CHANNELS[0]
    orch = PipelineOrchestrator(canal=canal)

    count = orch.phase_scrape()
    logger.info(f"Scraped {count} new items for {canal}")
    logger.info(f"Total unused content: {orch.db.get_unused_count(canal)}")
    return 0


def cmd_generate(args):
    """Generate scripts from existing scraped content."""
    from orchestrator import PipelineOrchestrator

    canal = args.canal or ACTIVE_CHANNELS[0]
    orch = PipelineOrchestrator(canal=canal)

    count = args.count or 1
    scripts = orch.script_gen.generate_batch(count)
    logger.info(f"Generated {len(scripts)} scripts for {canal}")
    for s in scripts:
        logger.info(f"  Script #{s['id']}: estimated {s.get('duracion_estimada', '?')} min")
    return 0


def cmd_upload(args):
    """Upload a video file to YouTube."""
    from orchestrator import PipelineOrchestrator

    canal = args.canal or ACTIVE_CHANNELS[0]
    orch = PipelineOrchestrator(canal=canal)

    if not orch.uploader.authenticate():
        logger.error("YouTube authentication failed. Check client_secret.json")
        return 1

    result = orch.uploader.upload(
        video_path=Path(args.video),
        title=args.title,
        description=args.description or "",
        tags=args.tags.split(",") if args.tags else [],
        thumbnail_path=Path(args.thumbnail) if args.thumbnail else None,
        privacy=args.privacy or "unlisted",
    )

    video_id = result.get("video_id")
    if video_id:
        logger.info(f"Uploaded: https://youtube.com/watch?v={video_id}")
    else:
        logger.error("Upload failed")
        return 1
    return 0


def cmd_stats(args):
    """Show database statistics."""
    canal = args.canal or ACTIVE_CHANNELS[0]
    db = Database()
    init_db()
    migrate_v2()

    logger.info(f"=== Autotube Stats for {canal} ===")
    logger.info(f"  Unused content:   {db.get_unused_count(canal)}")
    logger.info(f"  Videos today:     {db.get_videos_today(canal)}")

    # Total scripts
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row

    row = conn.execute("SELECT COUNT(*) as cnt FROM scripts WHERE canal = ?", (canal,)).fetchone()
    logger.info(f"  Total scripts:    {row['cnt']}")

    row = conn.execute("SELECT COUNT(*) as cnt FROM scripts WHERE canal = ? AND used = 1", (canal,)).fetchone()
    logger.info(f"  Scripts used:     {row['cnt']}")

    row = conn.execute("SELECT COUNT(*) as cnt FROM videos WHERE canal = ?", (canal,)).fetchone()
    logger.info(f"  Total videos:     {row['cnt']}")

    row = conn.execute("SELECT COUNT(*) as cnt FROM videos WHERE canal = ? AND yt_video_id IS NOT NULL", (canal,)).fetchone()
    logger.info(f"  Videos uploaded:  {row['cnt']}")

    # Recent pipeline log
    rows = conn.execute(
        "SELECT phase, status, message, created_at FROM pipeline_log WHERE canal = ? ORDER BY created_at DESC LIMIT 10",
        (canal,),
    ).fetchall()
    logger.info(f"  Recent pipeline log:")
    for r in rows:
        status_icon = "✓" if r["status"] == "success" else "✗" if r["status"] == "error" else "○"
        logger.info(f"    {status_icon} [{r['phase']}] {r['message'] or ''} ({r['created_at']})")

    conn.close()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Autotube — Automated YouTube Channel Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py run --canal canal2
  python main.py serve --canal canal2
  python main.py scrape --canal canal2
  python main.py generate --canal canal2 --count 3
  python main.py upload --video output/videos/video.mp4 --title "My Title"
  python main.py stats
        """,
    )

    parser.add_argument("--canal", type=str, help="Channel to operate on (default: first active channel)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # run
    p_run = subparsers.add_parser("run", help="Run full pipeline once")
    p_run.add_argument("--skip-upload", action="store_true", help="Skip YouTube upload")
    p_run.add_argument("--canal", type=str)

    # serve
    p_serve = subparsers.add_parser("serve", help="Run pipeline in scheduled mode")
    p_serve.add_argument("--canal", type=str)

    # scrape
    p_scrape = subparsers.add_parser("scrape", help="Scrape content only")
    p_scrape.add_argument("--canal", type=str)

    # generate
    p_gen = subparsers.add_parser("generate", help="Generate scripts from scraped content")
    p_gen.add_argument("--canal", type=str)
    p_gen.add_argument("--count", type=int, default=1, help="Number of scripts to generate")

    # upload
    p_upload = subparsers.add_parser("upload", help="Upload a video to YouTube")
    p_upload.add_argument("--canal", type=str)
    p_upload.add_argument("--video", type=str, required=True, help="Path to video file")
    p_upload.add_argument("--title", type=str, required=True, help="Video title")
    p_upload.add_argument("--description", type=str, help="Video description")
    p_upload.add_argument("--tags", type=str, help="Comma-separated tags")
    p_upload.add_argument("--thumbnail", type=str, help="Path to thumbnail image")
    p_upload.add_argument("--privacy", type=str, default="unlisted", choices=["public", "private", "unlisted"])

    # stats
    p_stats = subparsers.add_parser("stats", help="Show database statistics")
    p_stats.add_argument("--canal", type=str)

    args = parser.parse_args()

    setup_logging()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.command:
        parser.print_help()
        return 0

    commands = {
        "run": cmd_run,
        "serve": cmd_serve,
        "scrape": cmd_scrape,
        "generate": cmd_generate,
        "upload": cmd_upload,
        "stats": cmd_stats,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
