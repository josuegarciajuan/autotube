#!/usr/bin/env python3
"""Re-upload missing custom thumbnails to YouTube videos.

Reads diagnose_report_*.json and for each video without a custom thumbnail:
  1. If local thumbnail file exists on disk → re-upload directly to YT
  2. If no local file → regenerate with ThumbnailMaker (Pollo AI + gradient fallback)
  3. Upload to YT via thumbnails().set() (50 quota units)
  4. Update DB: thumbnail_verified = 1

SAFETY:
  - --dry-run: preview only, no uploads
  - --channel: limit to one channel
  - --max-upload: cap uploads per run
  - Only processes videos that exist in local DB (has db_video_id)

Usage:
    python3 scripts/reupload_missing_thumbnails.py --dry-run
    python3 scripts/reupload_missing_thumbnails.py --execute --channel canal3
    python3 scripts/reupload_missing_thumbnails.py --execute --max-upload 10
"""

import argparse
import json
import logging
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from database.db_extended import ExtendedDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reupload_thumbnails")

TOKENS_DIR = PROJECT_ROOT / "tokens"
OUTPUT_DIR = PROJECT_ROOT / "output"
QUOTA_PER_UPLOAD = 50       # thumbnails().set()
QUOTA_PER_VERIFY = 1        # thumbnails().list()


def _find_latest_report() -> Path | None:
    reports = sorted(OUTPUT_DIR.glob("diagnose_report_*.json"), reverse=True)
    return reports[0] if reports else None


def _authenticate(slug: str):
    token_path = TOKENS_DIR / f"{slug}.pickle"
    if not token_path.exists():
        logger.error("Token no encontrado: %s", token_path)
        return None
    try:
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    except Exception as exc:
        logger.error("Token corrupto para %s: %s", slug, exc)
        return None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleRequest())
                with open(token_path, "wb") as f:
                    pickle.dump(creds, f)
            except Exception as exc:
                logger.error("No se pudo refrescar token para %s: %s", slug, exc)
                return None
        else:
            logger.error("Token inválido para %s", slug)
            return None
    return build("youtube", "v3", credentials=creds)


def _has_local_thumbnail(db_video_id: int, db: ExtendedDatabase) -> Path | None:
    """Check if the video has a valid local thumbnail file."""
    v = db.get_video(db_video_id)
    if not v:
        return None
    tp = v.get("thumbnail_path", "")
    if tp and Path(tp).exists():
        return Path(tp)
    return None


def _regenerate_thumbnail(db_video_id: int, db: ExtendedDatabase) -> Path | None:
    """Regenerate thumbnail using ThumbnailMaker. Returns path or None."""
    try:
        from api.services.thumbnail_service import regenerate_thumbnail_for_video
        import asyncio

        # Run the async function in a new event loop
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(regenerate_thumbnail_for_video(db_video_id))
        finally:
            loop.close()

        if result:
            logger.info("  🎨 Thumbnail regenerated: %s", result)
            return Path(result)
        else:
            logger.warning("  ⚠️  Thumbnail regeneration returned None")
            return None
    except Exception as e:
        logger.warning("  ⚠️  Thumbnail regeneration failed: %s", e)
        return None


def _upload_thumbnail(service, yt_video_id: str, thumb_path: Path) -> bool:
    """Upload a thumbnail to YouTube. Returns True on success."""
    try:
        from googleapiclient.http import MediaFileUpload
        service.thumbnails().set(
            videoId=yt_video_id,
            media_body=MediaFileUpload(str(thumb_path), mimetype="image/jpeg"),
        ).execute()

        # Verify
        resp = service.thumbnails().list(videoId=yt_video_id).execute()
        if resp.get("items"):
            logger.info("  ✅ Thumbnail uploaded + verified for %s", yt_video_id)
            return True
        else:
            logger.warning("  ⚠️  Upload OK but verification empty for %s", yt_video_id)
            return False  # uncertain
    except HttpError as exc:
        logger.warning("  ❌ Thumbnail upload failed for %s: %s",
                       yt_video_id, str(exc)[:150])
        return False


def process_channel(slug: str, missing_thumbs: list, yt_service,
                    db: ExtendedDatabase, dry_run: bool,
                    max_upload: int) -> dict:
    """Re-upload missing thumbnails for one channel."""
    stats = {"uploaded": 0, "regenerated": 0, "failed": 0, "skipped": 0,
             "no_db_record": 0, "quota": 0}

    if not missing_thumbs:
        logger.info("[%s] No missing thumbnails", slug)
        return stats

    # Only process videos with DB records
    processable = [v for v in missing_thumbs if v.get("db_video_id")]
    no_db = len(missing_thumbs) - len(processable)

    print(f"\n{'='*60}")
    print(f"  Canal: {slug}")
    print(f"  Vídeos sin thumbnail: {len(missing_thumbs)}")
    print(f"  Procesables (en DB):  {len(processable)}")
    if no_db:
        print(f"  Huérfanos YT (sin DB): {no_db} (no se pueden procesar)")
    print(f"  Quota estimada:    ~{len(processable) * (QUOTA_PER_UPLOAD + 1)} unidades")
    print(f"  Modo: {'DRY-RUN' if dry_run else 'EJECUCIÓN REAL'}")
    print(f"{'='*60}")

    if not processable:
        stats["no_db_record"] = no_db
        return stats

    if not dry_run:
        confirm = input(f"\n¿Re-subir thumbnails para {len(processable)} vídeos de {slug}? (yes/no): ")
        if confirm.lower() not in ("yes", "y", "si", "sí"):
            logger.info("[%s] Cancelado por el usuario", slug)
            stats["skipped"] = len(processable)
            return stats

    remaining = max_upload if max_upload else 99999

    for idx, vid in enumerate(processable):
        if remaining <= 0:
            stats["skipped"] += len(processable) - idx
            break

        yt_id = vid["yt_video_id"]
        db_vid = vid["db_video_id"]
        title = vid.get("title", "")[:70]

        print(f"\n  [{idx+1}/{len(processable)}] {yt_id}")
        print(f"    \"{title}\"")

        if dry_run:
            print(f"    [DRY-RUN] Would attempt thumbnail re-upload")
            stats["uploaded"] += 1
            remaining -= 1
            continue

        # Step 1: Check local file
        local_thumb = _has_local_thumbnail(db_vid, db)
        if local_thumb:
            logger.info("  📁 Local thumbnail found: %s", local_thumb)
        else:
            logger.info("  🎨 No local thumbnail — regenerating via Pollo AI...")
            local_thumb = _regenerate_thumbnail(db_vid, db)
            if local_thumb:
                stats["regenerated"] += 1

        if not local_thumb:
            logger.warning("  ❌ No thumbnail available (no local + regeneration failed)")
            stats["failed"] += 1
            remaining -= 1
            continue

        # Step 2: Upload
        success = _upload_thumbnail(yt_service, yt_id, local_thumb)
        stats["quota"] += QUOTA_PER_UPLOAD + QUOTA_PER_VERIFY

        if success:
            stats["uploaded"] += 1
            try:
                db.update_video(db_vid, thumbnail_verified=1)
            except Exception:
                pass
            remaining -= 1
            time.sleep(0.5)  # Rate-limit
        else:
            stats["failed"] += 1
            remaining -= 1

    stats["no_db_record"] = no_db
    return stats


def run_reupload(report_path: Path = None, channel_filter: str = None,
                 dry_run: bool = True, max_upload: int = None) -> dict:
    """Re-upload missing thumbnails across channels."""
    if report_path is None:
        report_path = _find_latest_report()

    if report_path is None or not report_path.exists():
        logger.error("No diagnose report found. Run diagnose_all_channels.py first.")
        return {"error": "no_report"}

    logger.info("Loading report: %s", report_path)
    with open(report_path) as f:
        report = json.load(f)

    db = ExtendedDatabase()
    all_stats = {"total_uploaded": 0, "total_failed": 0, "total_skipped": 0,
                 "total_quota": 0, "channels": {}}

    channels = list(report.get("channels", {}).keys())
    if channel_filter:
        channels = [c for c in channels if c == channel_filter]

    for slug in channels:
        ch_data = report["channels"][slug]
        if ch_data.get("error"):
            logger.info("[%s] Skipping — auth error", slug)
            continue

        missing = ch_data.get("missing_thumbnail", [])
        if not missing:
            logger.info("[%s] No missing thumbnails", slug)
            continue

        yt_service = _authenticate(slug)
        if yt_service is None:
            logger.error("[%s] Auth failed", slug)
            continue

        stats = process_channel(slug, missing, yt_service, db, dry_run, max_upload)
        all_stats["total_uploaded"] += stats["uploaded"]
        all_stats["total_failed"] += stats["failed"]
        all_stats["total_skipped"] += stats["skipped"]
        all_stats["total_quota"] += stats["quota"]
        all_stats["channels"][slug] = stats

    return all_stats


def print_summary(stats: dict, dry_run: bool):
    print(f"\n{'='*60}")
    if dry_run:
        print(f"  DRY-RUN — Previsualización de thumbnails")
    else:
        print(f"  RESULTADO — Re-subida de thumbnails")
    print(f"{'='*60}")
    print(f"  Thumbnails subidos:    {stats['total_uploaded']}")
    print(f"  Fallos:                {stats['total_failed']}")
    print(f"  Saltados:              {stats['total_skipped']}")
    print(f"  Quota usada:           ~{stats['total_quota']} unidades")
    print(f"{'='*60}")
    if dry_run and stats["total_uploaded"] > 0:
        print(f"\n  Para ejecutar: python3 scripts/reupload_missing_thumbnails.py --execute")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-upload missing thumbnails to YouTube")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview only (default)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually upload thumbnails")
    parser.add_argument("--channel", type=str, default=None,
                        help="Process only one channel")
    parser.add_argument("--max-upload", type=int, default=None,
                        help="Max thumbnails to upload (cap)")
    parser.add_argument("--report", type=str, default=None,
                        help="Path to specific diagnose report JSON")
    args = parser.parse_args()

    dry_run = not args.execute
    report_path = Path(args.report) if args.report else None

    stats = run_reupload(
        report_path=report_path,
        channel_filter=args.channel,
        dry_run=dry_run,
        max_upload=args.max_upload,
    )

    if "error" in stats:
        logger.error("Aborted: %s", stats["error"])
        sys.exit(1)

    print_summary(stats, dry_run)
    sys.exit(0 if stats["total_failed"] == 0 else 1)
