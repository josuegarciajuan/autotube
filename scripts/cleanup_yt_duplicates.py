#!/usr/bin/env python3
"""Cleanup duplicate YouTube videos detected by diagnose_all_channels.py.

Reads diagnose_report_*.json and for each duplicate group:
  1. Keeps the video with the most views (canonical)
  2. Deletes the rest from YouTube via videos().delete() (50 quota units each)
  3. Updates DB: marks deleted videos as 'deleted_on_yt'

SAFETY FEATURES:
  - --dry-run: preview only, no deletions
  - --channel: limit to one channel
  - --max-delete: cap deletions per run
  - Confirmation prompt before each channel's deletions
  - Keeps at least 1 video per group (never orphans a topic)

Usage:
    python3 scripts/cleanup_yt_duplicates.py --dry-run          # preview only
    python3 scripts/cleanup_yt_duplicates.py --dry-run --channel canal2
    python3 scripts/cleanup_yt_duplicates.py --execute          # ACTUAL deletion
    python3 scripts/cleanup_yt_duplicates.py --execute --max-delete 20  # cap
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
logger = logging.getLogger("cleanup_duplicates")

TOKENS_DIR = PROJECT_ROOT / "tokens"
OUTPUT_DIR = PROJECT_ROOT / "output"
QUOTA_PER_DELETE = 50  # videos().delete cost
QUOTA_PER_UPDATE = 1   # videos().list cost during confirmation


def _find_latest_report() -> Path | None:
    """Find the most recent diagnose report."""
    reports = sorted(OUTPUT_DIR.glob("diagnose_report_*.json"), reverse=True)
    return reports[0] if reports else None


def _authenticate(slug: str):
    """Load OAuth2 token and return YouTube service."""
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


def _select_canonical(group: list[dict]) -> dict:
    """Select the canonical video to keep (most views, then earliest published).

    v24.1 fix: dedupe by yt_video_id FIRST. The diagnose report can contain
    the same video ID twice (pagination overlap), which previously caused the
    canonical video to also appear in `duplicates` and get DELETED. Deduping
    guarantees the kept video is never in the delete list.
    """
    if not group:
        raise ValueError("Empty duplicate group")

    # Dedupe by yt_video_id, keeping the first occurrence
    seen_ids = set()
    deduped = []
    for v in group:
        yt_id = v.get("yt_video_id")
        if yt_id and yt_id in seen_ids:
            continue
        if yt_id:
            seen_ids.add(yt_id)
        deduped.append(v)
    group = deduped

    # Sort: prefer videos already tracked in DB (has db_video_id), then by
    # views desc, then earliest published_at. This ensures the cleanup NEVER
    # deletes the DB-tracked video in favor of an orphan copy — critical when
    # a re-upload just completed and has 0 views while older orphan copies
    # exist.
    def _sort_key(v):
        in_db = 0 if v.get("db_video_id") else 1  # DB-tracked first
        views = v.get("views", 0)
        pub = v.get("published_at", "9999")
        return (in_db, -views, pub)

    sorted_group = sorted(group, key=_sort_key)
    canonical = sorted_group[0]
    duplicates = sorted_group[1:]

    return canonical, duplicates


def _delete_video(service, yt_video_id: str) -> bool:
    """Delete a YouTube video. Returns True on success."""
    try:
        service.videos().delete(id=yt_video_id).execute()
        logger.info("  🗑️  Deleted: %s", yt_video_id)
        return True
    except HttpError as exc:
        reason = str(exc)[:200]
        if "videoNotFound" in reason or "404" in reason:
            logger.info("  ⚠️  Already deleted / not found: %s (marking DB)", yt_video_id)
            return True  # Treat as success — already gone
        logger.error("  ❌ Failed to delete %s: %s", yt_video_id, reason)
        return False


def _mark_deleted_in_db(db: ExtendedDatabase, video: dict, slug: str):
    """Mark a video as deleted in local DB."""
    db_vid = video.get("db_video_id")
    yt_id = video["yt_video_id"]
    title = video.get("title", "")[:80]

    if db_vid:
        try:
            db.update_video(db_vid, status="deleted_on_yt",
                            progress_phase="cleanup_duplicate",
                            error_message=f"Deleted as duplicate of canonical video on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
            logger.info("  📝 DB updated: video #%d → deleted_on_yt", db_vid)
        except Exception as e:
            logger.warning("  ⚠️  DB update failed for #%d: %s", db_vid, e)
    else:
        logger.info("  ℹ️  No DB record for %s (%s) — already orphaned", yt_id, title)


def process_channel(slug: str, duplicates: list, yt_service,
                    db: ExtendedDatabase, dry_run: bool,
                    max_delete: int) -> dict:
    """Process all duplicate groups for one channel. Returns stats dict."""
    stats = {"groups": 0, "deleted": 0, "failed": 0, "skipped": 0, "quota": 0}

    if not duplicates:
        logger.info("[%s] No duplicate groups to process", slug)
        return stats

    # Safety: confirm with user
    total_to_delete = sum(max(0, len(group) - 1) for group in duplicates)
    print(f"\n{'='*60}")
    print(f"  Canal: {slug}")
    print(f"  Grupos duplicados: {len(duplicates)}")
    print(f"  Vídeos a borrar:   {total_to_delete}")
    print(f"  Quota estimada:    {total_to_delete * QUOTA_PER_DELETE} unidades")
    print(f"  Modo: {'DRY-RUN (sin borrar)' if dry_run else 'EJECUCIÓN REAL'}")
    print(f"{'='*60}")

    if total_to_delete == 0:
        return stats

    if not dry_run:
        confirm = input(f"\n¿Borrar {total_to_delete} vídeos duplicados de {slug}? (yes/no): ")
        if confirm.lower() not in ("yes", "y", "si", "sí"):
            logger.info("[%s] Cancelado por el usuario", slug)
            stats["skipped"] = total_to_delete
            return stats

    remaining_deletes = max_delete if max_delete else 99999

    for group_idx, group in enumerate(duplicates):
        if remaining_deletes <= 0:
            logger.info("[%s] Max delete cap reached (%d) — stopping", slug, max_delete)
            stats["skipped"] += sum(max(0, len(g) - 1) for g in duplicates[group_idx:])
            break

        canonical, to_delete = _select_canonical(group)

        print(f"\n  Grupo #{group_idx+1} ({len(group)} copias)")
        print(f"    ✨ KEEP:  {canonical['yt_video_id']} ({canonical.get('views', 0):,} views)")
        print(f"            \"{canonical.get('title', '')[:70]}\"")

        for vid in to_delete:
            if remaining_deletes <= 0:
                stats["skipped"] += 1
                continue

            print(f"    🗑️  DEL:   {vid['yt_video_id']} ({vid.get('views', 0):,} views)")

            if not dry_run:
                success = _delete_video(yt_service, vid["yt_video_id"])
                stats["quota"] += QUOTA_PER_DELETE

                if success:
                    _mark_deleted_in_db(db, vid, slug)
                    stats["deleted"] += 1
                    remaining_deletes -= 1
                    time.sleep(0.5)  # Rate-limit: don't hammer YT API
                else:
                    stats["failed"] += 1
            else:
                stats["deleted"] += 1  # counted as "would delete"
                remaining_deletes -= 1

        stats["groups"] += 1

    return stats


def run_cleanup(report_path: Path = None, channel_filter: str = None,
                dry_run: bool = True, max_delete: int = None) -> dict:
    """Run full cleanup of duplicates across all (or one) channels."""
    if report_path is None:
        report_path = _find_latest_report()

    if report_path is None or not report_path.exists():
        logger.error("No diagnose report found. Run diagnose_all_channels.py first.")
        return {"error": "no_report"}

    logger.info("Loading report: %s", report_path)
    with open(report_path) as f:
        report = json.load(f)

    db = ExtendedDatabase()
    all_stats = {"total_deleted": 0, "total_failed": 0, "total_skipped": 0,
                 "total_quota": 0, "channels": {}}

    channels = list(report.get("channels", {}).keys())
    if channel_filter:
        channels = [c for c in channels if c == channel_filter]

    for slug in channels:
        ch_data = report["channels"][slug]
        if ch_data.get("error"):
            logger.info("[%s] Skipping — auth error in report", slug)
            continue

        duplicates = ch_data.get("duplicates", [])
        if not duplicates:
            logger.info("[%s] No duplicates", slug)
            continue

        yt_service = _authenticate(slug)
        if yt_service is None:
            logger.error("[%s] Auth failed — skipping", slug)
            continue

        stats = process_channel(slug, duplicates, yt_service, db, dry_run, max_delete)
        all_stats["total_deleted"] += stats["deleted"]
        all_stats["total_failed"] += stats["failed"]
        all_stats["total_skipped"] += stats["skipped"]
        all_stats["total_quota"] += stats["quota"]
        all_stats["channels"][slug] = stats

    return all_stats


def print_final_summary(stats: dict, dry_run: bool):
    """Print final summary after all channels."""
    prefix = "[DRY-RUN] Would have" if dry_run else ""
    print(f"\n{'='*60}")
    print(f"  {'DRY-RUN — ' if dry_run else ''}LIMPIEZA COMPLETADA")
    print(f"{'='*60}")
    print(f"  Vídeos borrados:      {stats['total_deleted']}")
    print(f"  Fallos:               {stats['total_failed']}")
    print(f"  Saltados (cap):       {stats['total_skipped']}")
    print(f"  Quota total usada:    ~{stats['total_quota']} unidades")
    print(f"{'='*60}")

    if dry_run and stats["total_deleted"] > 0:
        print(f"\n  ✅ Dry-run exitoso. {stats['total_deleted']} vídeos marcados para borrar.")
        print(f"  Para ejecutar: python3 scripts/cleanup_yt_duplicates.py --execute")
    elif not dry_run:
        print(f"\n  ✅ {stats['total_deleted']} duplicados eliminados de YouTube.")
        if stats["total_failed"] > 0:
            print(f"  ⚠️  {stats['total_failed']} fallos — revisa los logs.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Cleanup duplicate YouTube videos across all channels")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview only (default)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete videos from YouTube")
    parser.add_argument("--channel", type=str, default=None,
                        help="Process only one channel (e.g. canal2)")
    parser.add_argument("--max-delete", type=int, default=None,
                        help="Max videos to delete per channel (safety cap)")
    parser.add_argument("--report", type=str, default=None,
                        help="Path to specific diagnose report JSON")
    args = parser.parse_args()

    dry_run = not args.execute

    report_path = Path(args.report) if args.report else None

    stats = run_cleanup(
        report_path=report_path,
        channel_filter=args.channel,
        dry_run=dry_run,
        max_delete=args.max_delete,
    )

    if "error" in stats:
        logger.error("Cleanup aborted: %s", stats["error"])
        sys.exit(1)

    print_final_summary(stats, dry_run)
    sys.exit(0 if stats["total_failed"] == 0 else 1)
