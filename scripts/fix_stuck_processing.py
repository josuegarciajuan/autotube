#!/usr/bin/env python3
"""Fix videos stuck in YouTube processing or failed publish status.

Reads diagnose_report_*.json and for each stuck/failed video:
  1. Checks processingStatus via YT API
  2. If processingStatus=processing and >24h: mark as stuck_processing in DB
  3. If processingStatus=succeeded but privacy=private with publishAt passed:
     force go_public
  4. Provides suggested actions for the operator

Usage:
    python3 scripts/fix_stuck_processing.py --dry-run
    python3 scripts/fix_stuck_processing.py --execute --channel canal2
"""

import argparse
import json
import logging
import pickle
import sys
import time
from datetime import datetime, timezone, timedelta
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
logger = logging.getLogger("fix_processing")

TOKENS_DIR = PROJECT_ROOT / "tokens"
OUTPUT_DIR = PROJECT_ROOT / "output"


def _find_latest_report() -> Path | None:
    reports = sorted(OUTPUT_DIR.glob("diagnose_report_*.json"), reverse=True)
    return reports[0] if reports else None


def _authenticate(slug: str):
    token_path = TOKENS_DIR / f"{slug}.pickle"
    if not token_path.exists():
        return None
    try:
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    except Exception:
        return None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleRequest())
                with open(token_path, "wb") as f:
                    pickle.dump(creds, f)
            except Exception:
                return None
        else:
            return None
    return build("youtube", "v3", credentials=creds)


def _check_video_status(service, yt_video_id: str) -> dict:
    """Get current processing and privacy status from YT API. 1 quota unit."""
    try:
        resp = service.videos().list(
            part="status,snippet",
            id=yt_video_id,
        ).execute()
        items = resp.get("items", [])
        if not items:
            return {"found": False, "reason": "video_not_found"}
        status = items[0].get("status", {})
        snippet = items[0].get("snippet", {})
        return {
            "found": True,
            "processing_status": status.get("processingStatus", ""),
            "privacy_status": status.get("privacyStatus", ""),
            "upload_status": status.get("uploadStatus", ""),
            "publish_at": status.get("publishAt", ""),
            "title": snippet.get("title", "")[:80],
        }
    except HttpError as exc:
        return {"found": False, "reason": str(exc)[:200]}


def _force_go_public(service, yt_video_id: str) -> bool:
    """Force video privacy to public. 50 quota units."""
    try:
        service.videos().update(
            part="status",
            body={"id": yt_video_id, "status": {"privacyStatus": "public"}},
        ).execute()
        logger.info("  ✅ Forced public: %s", yt_video_id)
        return True
    except HttpError as exc:
        logger.error("  ❌ Force public failed for %s: %s", yt_video_id, str(exc)[:150])
        return False


def process_stuck_videos(slug: str, stuck_list: list, publish_failed: list,
                         yt_service, db: ExtendedDatabase, dry_run: bool) -> dict:
    """Process stuck and publish-failed videos for one channel."""
    stats = {"fixed": 0, "marked_stuck": 0, "already_ok": 0, "failed": 0, "quota": 0}

    all_videos = []

    # Add stuck_processing entries
    for v in stuck_list:
        all_videos.append({"type": "stuck_processing", **v})

    # Add publish_failed entries
    for v in publish_failed:
        all_videos.append({"type": "publish_failed", **v})

    # ── Also scan uploaded_private videos with past target_public_at from DB ──
    try:
        with db._connect() as conn:
            conn.row_factory = None
            past_due = conn.execute(
                """SELECT v.id, v.yt_video_id, v.titulo_final, v.target_public_at,
                          v.status, v.publish_mode
                   FROM videos v
                   WHERE v.status = 'uploaded_private'
                     AND v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
                     AND v.target_public_at IS NOT NULL
                     AND v.target_public_at <= datetime('now')
                   ORDER BY v.target_public_at ASC"""
            ).fetchall()
        for row in past_due:
            db_id, yt_id, title, tpa, status, pub_mode = row
            # Check if already in our list
            if not any(v.get("yt_video_id") == yt_id for v in all_videos):
                all_videos.append({
                    "type": "past_due",
                    "yt_video_id": yt_id,
                    "title": title or "",
                    "db_video_id": db_id,
                    "in_db": True,
                    "hours_past": round((datetime.now(timezone.utc) - datetime.fromisoformat(
                        str(tpa).replace("Z", "+00:00").replace(" ", "T")
                    )).total_seconds() / 3600, 1),
                    "db_status": status,
                })
    except Exception as e:
        logger.debug("[%s] Could not query past-due videos: %s", slug, e)

    if not all_videos:
        logger.info("[%s] No stuck/failed/past-due videos", slug)
        return stats

    print(f"\n{'='*60}")
    print(f"  Canal: {slug}")
    print(f"  Vídeos a verificar: {len(all_videos)}")
    print(f"  Modo: {'DRY-RUN' if dry_run else 'EJECUCIÓN REAL'}")
    print(f"{'='*60}")

    for idx, vid in enumerate(all_videos):
        yt_id = vid["yt_video_id"]
        vid_type = vid.get("type", "unknown")
        title = vid.get("title", "")[:60]
        db_vid = vid.get("db_video_id")

        print(f"\n  [{idx+1}/{len(all_videos)}] {vid_type}: {yt_id}")
        print(f"    \"{title}\"")

        # Check current YT status (1 quota)
        status = _check_video_status(yt_service, yt_id)
        stats["quota"] += 1

        if not status.get("found"):
            print(f"    ⚠️  Video not found on YT — may be deleted")
            if not dry_run and db_vid:
                db.update_video(db_vid, status="deleted_on_yt",
                                progress_phase="cleanup")
                stats["already_ok"] += 1
            continue

        ps = status["processing_status"]
        pr = status["privacy_status"]

        print(f"    processingStatus: {ps}, privacyStatus: {pr}")

        if pr == "public":
            print(f"    ✅ Already public — marking as published in DB")
            if not dry_run and db_vid:
                db.update_video(db_vid, status="published",
                                published_verified_at=datetime.now(timezone.utc).isoformat())
                stats["already_ok"] += 1
            continue

        if ps == "processing" or (ps == "" and status["upload_status"] not in ("processed",)):
            hours_str = vid.get("hours_stuck") or vid.get("hours_past")
            if hours_str:
                print(f"    🔵 Still processing ({hours_str}h) — marking stuck_processing")
            else:
                print(f"    🔵 Still processing — marking stuck_processing")
            if not dry_run and db_vid:
                db.update_video(
                    db_vid, status="stuck_processing",
                    progress_phase="yt_processing_stuck",
                    published_retry_at=None,
                    error_message=f"YouTube processing stuck for {hours_str or 'unknown'}h"
                )
                stats["marked_stuck"] += 1
            continue

        # Processing done but video not public → force go_public
        if ps in ("succeeded", "processed", "") and pr != "public":
            print(f"    🟢 Processing done (status={ps}) — forcing go_public")
            if not dry_run:
                if _force_go_public(yt_service, yt_id):
                    stats["quota"] += 50
                    if db_vid:
                        db.update_video(
                            db_vid, status="published",
                            privacy_status="public",
                            published_verified_at=datetime.now(timezone.utc).isoformat(),
                        )
                    stats["fixed"] += 1
                else:
                    stats["failed"] += 1
            else:
                stats["fixed"] += 1
            continue

        # Unknown state
        print(f"    ⚠️  Unknown state — skipping")

    return stats


def run_fix(report_path: Path = None, channel_filter: str = None,
            dry_run: bool = True) -> dict:
    if report_path is None:
        report_path = _find_latest_report()
    if report_path is None or not report_path.exists():
        logger.error("No diagnose report found.")
        return {"error": "no_report"}

    logger.info("Loading report: %s", report_path)
    with open(report_path) as f:
        report = json.load(f)

    db = ExtendedDatabase()
    all_stats = {"total_fixed": 0, "total_marked_stuck": 0,
                 "total_already_ok": 0, "total_failed": 0,
                 "total_quota": 0, "channels": {}}

    channels = list(report.get("channels", {}).keys())
    if channel_filter:
        channels = [c for c in channels if c == channel_filter]

    for slug in channels:
        ch_data = report["channels"][slug]
        if ch_data.get("error"):
            continue

        stuck = ch_data.get("stuck_processing", [])
        failed = ch_data.get("publish_failed", [])

        yt_service = _authenticate(slug)
        if yt_service is None:
            continue

        stats = process_stuck_videos(slug, stuck, failed, yt_service, db, dry_run)
        all_stats["total_fixed"] += stats["fixed"]
        all_stats["total_marked_stuck"] += stats["marked_stuck"]
        all_stats["total_already_ok"] += stats["already_ok"]
        all_stats["total_failed"] += stats["failed"]
        all_stats["total_quota"] += stats["quota"]
        all_stats["channels"][slug] = stats

    return all_stats


def print_summary(stats: dict, dry_run: bool):
    print(f"\n{'='*60}")
    print(f"  {'DRY-RUN — ' if dry_run else ''}PROCESSING FIX")
    print(f"{'='*60}")
    print(f"  Forzados a public:     {stats['total_fixed']}")
    print(f"  Marcados stuck:        {stats['total_marked_stuck']}")
    print(f"  Ya estaban OK:         {stats['total_already_ok']}")
    print(f"  Fallos:                {stats['total_failed']}")
    print(f"  Quota usada:           ~{stats['total_quota']} unidades")
    print(f"{'='*60}")
    if stats["total_marked_stuck"] > 0:
        print(f"\n  ⚠️  {stats['total_marked_stuck']} vídeos marcados como stuck_processing.")
        print(f"  Acción manual: ir a YouTube Studio → verificar → borrar y re-subir si es necesario.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix stuck processing videos")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview only (default)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually fix videos")
    parser.add_argument("--channel", type=str, default=None,
                        help="Process only one channel")
    parser.add_argument("--report", type=str, default=None,
                        help="Path to diagnose report JSON")
    args = parser.parse_args()

    dry_run = not args.execute
    report_path = Path(args.report) if args.report else None

    stats = run_fix(report_path=report_path, channel_filter=args.channel, dry_run=dry_run)

    if "error" in stats:
        logger.error("Aborted: %s", stats["error"])
        sys.exit(1)

    print_summary(stats, dry_run)
    sys.exit(0 if stats["total_failed"] == 0 else 1)
