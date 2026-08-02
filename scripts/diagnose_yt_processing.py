#!/usr/bin/env python3
"""Diagnóstico de procesamiento de YouTube.

Cruza todos los yt_video_id en la DB contra YouTube API para detectar:
- processingStatus = failed    → "procesamiento interrumpido"
- uploadStatus = failed        → upload falló
- Video no encontrado          → eliminado por YouTube
- processingStatus = processing → aún procesando
- privacyStatus != public con target vencido → pendiente de publicación

Uso:
    python3 scripts/diagnose_yt_processing.py
    python3 scripts/diagnose_yt_processing.py --csv     # exporta CSV también
    python3 scripts/diagnose_yt_processing.py --channel canal2  # solo un canal
"""

import csv
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from database.db_extended import ExtendedDatabase
from pipeline.youtube_uploader import YouTubeUploader

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("diagnose_yt")

OUTPUT_DIR = Path(__file__).parent.parent / "output"
YOUTUBE_API_COST_PER_VIDEO = 1  # quota units per videos.list call


def get_channel_slug_for_video(video: dict, db: ExtendedDatabase) -> str:
    """Resolve channel slug for a video, either from canal field or channel_id."""
    slug = video.get("canal", "")
    if slug:
        return slug
    ch_id = video.get("channel_id")
    if ch_id:
        channels = db.get_channels()
        for ch in channels:
            if ch.get("id") == ch_id:
                return ch.get("slug", "")
    return ""


def diagnose_videos(db: ExtendedDatabase, channel_filter: str = None) -> list[dict]:
    """Query videos with YT IDs not published, classify by YouTube API response."""
    videos = db.get_videos(status=None, limit=9999, offset=0)

    target: list[dict] = []
    for v in videos:
        yt_id_raw = v.get("yt_video_id")
        if yt_id_raw is None:
            continue
        yt_id = str(yt_id_raw).strip()
        if not yt_id:
            continue
        status = v.get("status", "")
        if status == "published":
            # Already confirmed published → skip (but verify if privacy_status mismatch)
            if v.get("privacy_status") != "public":
                target.append(v)
            continue
        if status in ("uploaded", "uploaded_private", "private_quality_issue",
                       "unlisted", "draft", "ready", "awaiting_upload", "error"):
            target.append(v)

    # Filter by channel if requested
    if channel_filter:
        target = [v for v in target
                  if get_channel_slug_for_video(v, db) == channel_filter
                  or v.get("canal") == channel_filter]

    return target


def verify_yt_status(uploader: YouTubeUploader, yt_video_id: str) -> dict | None:
    """Query YouTube API for a single video's status and processing info.

    Returns dict with keys:
        found: bool
        title: str
        privacyStatus: str
        uploadStatus: str
        processingStatus: str
        processingFailureReason: str
        failureReason: str
        rejectionReason: str
        raw: full API response snippet
    Or None if API call failed (auth/quota).
    """
    try:
        service = uploader._get_service()
        resp = service.videos().list(
            part="status,snippet,processingDetails",
            id=yt_video_id,
        ).execute()
        items = resp.get("items", [])
        if not items:
            return {"found": False, "raw": resp}

        item = items[0]
        snippet = item.get("snippet", {})
        status = item.get("status", {})
        processing = item.get("processingDetails", {})

        return {
            "found": True,
            "title": snippet.get("title", ""),
            "privacyStatus": status.get("privacyStatus", ""),
            "uploadStatus": status.get("uploadStatus", ""),
            "processingStatus": processing.get("processingStatus", ""),
            "processingFailureReason": processing.get("processingFailureReason", ""),
            "failureReason": status.get("failureReason", ""),
            "rejectionReason": status.get("rejectionReason", ""),
            "raw": item,
            "madeForKids": status.get("madeForKids", False),
            "selfDeclaredMadeForKids": status.get("selfDeclaredMadeForKids", False),
        }
    except Exception as exc:
        logger.warning(f"    ⚠️ API error for {yt_video_id}: {str(exc)[:120]}")
        return None


def classify_result(yt_info: dict | None) -> tuple[str, str]:
    """Classify YouTube API result into action and icon."""
    if yt_info is None:
        return "⚠️ API error — reintentar más tarde", "API_ERROR"

    if not yt_info["found"]:
        return "💀 ELIMINADO por YouTube (no encontrado)", "DELETED"

    ps = yt_info.get("processingStatus", "")
    pf = yt_info.get("processingFailureReason", "")
    us = yt_info.get("uploadStatus", "")
    fr = yt_info.get("failureReason", "")
    rr = yt_info.get("rejectionReason", "")

    if ps == "failed":
        return f"🔴 RE-SUBIR (processing failed: {pf or 'desconocido'})", "PROCESSING_FAILED"
    if us == "failed" or fr:
        return f"🔴 RE-SUBIR (upload/rejection: {fr or us})", "UPLOAD_FAILED"
    if rr:
        return f"🚫 Rechazado: {rr}", "REJECTED"
    if ps == "processing" or (us == "uploaded" and not ps):
        return "⏳ Procesando aún — esperar", "PROCESSING"
    if ps == "succeeded" or us == "processed":
        # Video está OK en YouTube — verificar si la DB necesita sincronización
        privacy = yt_info.get("privacyStatus", "")
        if privacy == "public":
            return "✅ Ya es público (sincronizar DB)", "OK_PUBLIC"
        else:
            return f"✅ OK (privacy={privacy}) — esperando publicación", "OK_PRIVATE"

    return f"❓ Estado desconocido: us={us}, ps={ps}", "UNKNOWN"


def run_diagnosis(export_csv: bool = False, channel_filter: str = None):
    """Main diagnosis: iterate videos, query YouTube, build report."""
    db = ExtendedDatabase()
    videos = diagnose_videos(db, channel_filter)
    total = len(videos)

    if total == 0:
        logger.info("✅ No se encontraron videos con problemas. Todo OK.")
        return []

    logger.info("=" * 100)
    logger.info(f"🔍 DIAGNÓSTICO DE PROCESAMIENTO YOUTUBE — {total} videos a verificar")
    logger.info(f"📊 Quota estimada: ~{total} unidades de YouTube API")
    logger.info("=" * 100)

    # Group by channel to reuse uploader
    channel_uploaders: dict[str, YouTubeUploader] = {}
    channel_map: dict[str, str] = {}  # channel_slug → display name

    # Resolve all channels first
    channels = db.get_channels()
    for ch in channels:
        channel_map[ch.get("slug", "")] = ch.get("name", ch.get("slug", ""))

    results = []
    stats = {
        "total": total,
        "ok_public": 0,
        "ok_private": 0,
        "processing": 0,
        "processing_failed": 0,
        "upload_failed": 0,
        "deleted": 0,
        "rejected": 0,
        "api_error": 0,
        "unknown": 0,
    }

    for idx, v in enumerate(videos, 1):
        v_id = v["id"]
        yt_id = v.get("yt_video_id", "")
        channel = get_channel_slug_for_video(v, db) or "?"
        title = v.get("titulo_final") or v.get("title") or "?"
        db_status = v.get("status", "")
        target_at = v.get("target_public_at", "")
        privacy = v.get("privacy_status", "")

        # Get or create uploader for this channel
        if channel not in channel_uploaders:
            uploader = YouTubeUploader(channel)
            if not uploader.authenticate():
                logger.error(f"❌ No se pudo autenticar para canal '{channel}' — saltando")
                channel_uploaders[channel] = None
                continue
            channel_uploaders[channel] = uploader

        uploader = channel_uploaders[channel]
        if uploader is None:
            continue

        # Query YouTube API
        logger.info(f"[{idx}/{total}] ID={v_id} canal={channel} yt_id={yt_id} …")
        yt_info = verify_yt_status(uploader, yt_id)

        action_text, action_code = classify_result(yt_info)
        row = {
            "db_id": v_id,
            "canal": channel,
            "yt_video_id": yt_id,
            "titulo": title[:80] if title else "?",
            "db_status": db_status,
            "db_privacy": privacy,
            "target_public_at": target_at,
            "yt_uploadStatus": yt_info.get("uploadStatus", "?") if yt_info else "?",
            "yt_processingStatus": yt_info.get("processingStatus", "?") if yt_info else "?",
            "yt_privacyStatus": yt_info.get("privacyStatus", "?") if yt_info else "?",
            "action": action_text,
            "action_code": action_code,
        }
        results.append(row)

        # Update stats
        if action_code in stats:
            stats[action_code] = stats.get(action_code, 0) + 1

        # Print per-video line
        icon = action_text.split(" ", 1)[0] if action_text else "?"
        logger.info(f"    {icon} {action_text}")

    # ── Summary ──
    logger.info("")
    logger.info("=" * 100)
    logger.info("📊 RESUMEN")
    logger.info("=" * 100)
    for label, key in [
        ("✅ Ya públicos (DB desactualizada)", "ok_public"),
        ("✅ OK esperando publicación", "ok_private"),
        ("⏳ Procesando aún", "processing"),
        ("🔴 Procesamiento fallido", "processing_failed"),
        ("🔴 Upload fallido", "upload_failed"),
        ("💀 Eliminados por YouTube", "deleted"),
        ("🚫 Rechazados", "rejected"),
        ("⚠️ Error de API (quota/auth)", "api_error"),
        ("❓ Desconocido", "unknown"),
    ]:
        count = stats.get(key, 0)
        if count > 0:
            logger.info(f"  {label}: {count}")

    # ── CSV export ──
    if export_csv and results:
        OUTPUT_DIR.mkdir(exist_ok=True)
        csv_path = OUTPUT_DIR / "diagnostico_yt_processing.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "db_id", "canal", "yt_video_id", "titulo",
                "db_status", "db_privacy", "target_public_at",
                "yt_uploadStatus", "yt_processingStatus", "yt_privacyStatus",
                "action", "action_code",
            ])
            writer.writeheader()
            writer.writerows(results)
        logger.info(f"\n📁 CSV exportado → {csv_path}")

    logger.info(f"\n📊 Quota total usada: ~{total} unidades (de 10,000 diarias)")
    return results


if __name__ == "__main__":
    export = "--csv" in sys.argv
    channel = None
    for i, arg in enumerate(sys.argv):
        if arg == "--channel" and i + 1 < len(sys.argv):
            channel = sys.argv[i + 1]
            break

    run_diagnosis(export_csv=export, channel_filter=channel)
