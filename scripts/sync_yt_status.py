#!/usr/bin/env python3
"""Sincronización de estados DB ↔ YouTube real.

Recorre todos los videos con yt_video_id y cruza contra YouTube API
(part=status,snippet) para corregir estados inconsistentes:

- published en YT pero no en DB → actualizar a published
- Eliminados/rechazados en YT → marcar en DB
- target_public_at vencido + privacy != public → reportar
- privacy_status en DB ≠ privacy en YT → corregir

Uso:
    python3 scripts/sync_yt_status.py          # dry-run (solo reporta)
    python3 scripts/sync_yt_status.py --apply  # aplica cambios
    python3 scripts/sync_yt_status.py --channel canal2  # solo un canal
"""

import logging
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from database.db_extended import ExtendedDatabase
from pipeline.youtube_uploader import YouTubeUploader

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sync_yt")


def sync_videos(dry_run: bool = True, channel_filter: str = None) -> dict:
    """Sync all videos with YT IDs against YouTube's real state."""
    db = ExtendedDatabase()
    videos = db.get_videos(status=None, limit=9999, offset=0)

    channels = db.get_channels()
    channel_slugs = {ch["id"]: ch["slug"] for ch in channels}

    # Filter: videos with yt_video_id
    target = [v for v in videos
              if v.get("yt_video_id") and str(v.get("yt_video_id", "")).strip()]
    if channel_filter:
        target = [v for v in target
                  if v.get("canal") == channel_filter
                  or channel_slugs.get(v.get("channel_id", 0)) == channel_filter]

    channel_uploaders = {}
    stats = {"total": len(target), "fixed_published": 0, "fixed_privacy": 0,
             "marked_deleted": 0, "marked_failed": 0, "ok": 0, "errors": 0}

    for v in target:
        v_id = v["id"]
        yt_id = v["yt_video_id"]
        canal = v.get("canal") or channel_slugs.get(v.get("channel_id", 0), "?")
        db_status = v.get("status", "")
        db_privacy = v.get("privacy_status", "")

        if canal not in channel_uploaders:
            uploader = YouTubeUploader(canal)
            if uploader.authenticate():
                channel_uploaders[canal] = uploader
            else:
                channel_uploaders[canal] = None
        uploader = channel_uploaders[canal]
        if uploader is None:
            continue

        try:
            service = uploader._get_service()
            resp = service.videos().list(
                part="status,snippet", id=yt_id
            ).execute()
            items = resp.get("items", [])
        except Exception as exc:
            logger.warning(f"  ⚠️ API error for {yt_id}: {str(exc)[:80]}")
            stats["errors"] += 1
            continue

        if not items:
            # Video not found — was deleted from YouTube
            logger.warning(f"  💀 {v_id} ({canal}) yt={yt_id}: ELIMINADO de YouTube")
            if not dry_run and db_status not in ("deleted", "published"):
                db.update_video(v_id, status="deleted",
                                progress_phase="yt_deleted")
            stats["marked_deleted"] += 1
            continue

        item = items[0]
        yt_privacy = item["status"].get("privacyStatus", "")
        yt_upload = item["status"].get("uploadStatus", "")
        yt_processing = item["status"].get("processingStatus", "")
        yt_failure = item["status"].get("failureReason", "")
        yt_title = item["snippet"].get("title", "")

        # ── Mark published if YT says public but DB doesn't ──
        if yt_privacy == "public" and db_status != "published":
            logger.info(f"  ✅ {v_id} ({canal}) yt={yt_id}: Ya es público → actualizando DB")
            if not dry_run:
                db.update_video(v_id, status="published", privacy_status="public",
                                published_at=datetime.now(timezone.utc).isoformat())
            stats["fixed_published"] += 1
            continue

        # ── Fix privacy mismatch ──
        if yt_privacy and yt_privacy != db_privacy:
            logger.info(f"  🔄 {v_id} ({canal}) yt={yt_id}: privacy DB={db_privacy} YT={yt_privacy}")
            if not dry_run:
                db.update_video(v_id, privacy_status=yt_privacy)
            stats["fixed_privacy"] += 1

        # ── Mark failed if YT shows processing failed ──
        if yt_processing == "failed" or yt_upload == "failed" or yt_failure:
            reason = yt_failure or yt_processing or yt_upload
            logger.warning(f"  🔴 {v_id} ({canal}) yt={yt_id}: FAILED ({reason})")
            if not dry_run and db_status not in ("error", "deleted"):
                if yt_processing == "failed":
                    from api.services.upload_health_checker import _auto_retry_upload
                    success = _auto_retry_upload(v_id, yt_id, canal, db, reason)
                    if success:
                        logger.info(f"  ✅ Auto-retry launched for video #{v_id}")
                    else:
                        db.update_video(v_id, status="error",
                                        progress_phase="yt_processing_failed")
                else:
                    db.update_video(v_id, status="error",
                                    progress_phase=f"yt_{yt_upload or 'failed'}")
            stats["marked_failed"] += 1
            continue

        stats["ok"] += 1

    # Summary
    logger.info(f"\n📊 Sincronización {'[DRY-RUN] ' if dry_run else ''}completada:")
    logger.info(f"  Total verificados: {stats['total']}")
    logger.info(f"  ✅ Ya públicos (DB actualizada): {stats['fixed_published']}")
    logger.info(f"  🔄 Privacy corregido: {stats['fixed_privacy']}")
    logger.info(f"  💀 Eliminados: {stats['marked_deleted']}")
    logger.info(f"  🔴 Fallos: {stats['marked_failed']}")
    logger.info(f"  ✅ OK: {stats['ok']}")
    logger.info(f"  ⚠️ Errores API: {stats['errors']}")

    return stats


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    channel = None
    for i, arg in enumerate(sys.argv):
        if arg == "--channel" and i + 1 < len(sys.argv):
            channel = sys.argv[i + 1]
            break

    if apply:
        logger.info("⚠️  MODO --apply: los cambios se aplicarán en la DB")
    else:
        logger.info("🔍 MODO DRY-RUN: solo reportando (usa --apply para aplicar)")

    sync_videos(dry_run=not apply, channel_filter=channel)
