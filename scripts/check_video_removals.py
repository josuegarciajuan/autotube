#!/usr/bin/env python3
"""Detección de eliminaciones silenciosas de YouTube (0 cuota).

YouTube elimina vídeos retroactivamente ("La niña que no siente dolor" pasó la
verificación de subida y fue borrada horas/días después sin que el sistema lo
viera). Este script barre los últimos N vídeos y shorts publicados de cada
canal y comprueba su estado público vía la watch page (sin consumir cuota de la
Data API). Si un vídeo consta como publicado en DB pero YouTube lo reporta como
eliminado, crea una pipeline_alert de riesgo.

Uso:
    python3 scripts/check_video_removals.py              # barrido (default 14 días)
    python3 scripts/check_video_removals.py --days 7
    python3 scripts/check_video_removals.py --dry-run     # solo imprimir, no alertar

Pensado para ejecutarse desde un cron diario.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("check_video_removals")

ALERTED_KEY = "removal_sweep_alerted_ids"


def _connect():
    from config.settings import DATABASE_PATH
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def watch_page_status(video_id: str) -> str:
    """Clasifica el estado público del vídeo (sin cuota)."""
    try:
        url = f"https://www.youtube.com/watch?v={video_id}&hl=es"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "es-ES,es;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read(250_000).decode("utf-8", "ignore")
    except Exception:
        return "unknown"
    if '"status":"OK"' in html or '"status":"LIVE_STREAM_OFFLINE"' in html:
        return "available"
    if '"status":"LOGIN_REQUIRED"' in html:
        return "private"
    for marker in (
        "Este vídeo no está disponible",
        "Este video no está disponible",
        "El vídeo no está disponible",
        "This video isn't available anymore",
        "Video unavailable",
        "This video has been removed",
    ):
        if marker in html:
            return "removed"
    return "unknown"


def _already_alerted(db, video_id: str) -> bool:
    try:
        raw = db.get_system_state(ALERTED_KEY) or "{}"
        ids = set(json.loads(raw))
        return video_id in ids
    except Exception:
        return False


def _mark_alerted(db, video_id: str) -> None:
    try:
        raw = db.get_system_state(ALERTED_KEY) or "{}"
        ids = set(json.loads(raw))
        ids.add(video_id)
        # Mantener acotado (últimos 2000).
        db.set_system_state(ALERTED_KEY, json.dumps(sorted(ids)[-2000:]))
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from database.db_extended import ExtendedDatabase
    from api.services.lifecycle_monitor import create_alert

    db = ExtendedDatabase()
    conn = _connect()

    # 1. Vídeos long-form recientes (publicados o subidos private programados)
    vids = conn.execute(
        """SELECT id, canal, channel_id, yt_video_id, titulo_final, status
             FROM videos
            WHERE yt_video_id IS NOT NULL AND yt_video_id != ''
              AND status IN ('published','uploaded_private','uploaded')
              AND date(uploaded_at) >= date('now', 'localtime', ?)
            ORDER BY id DESC""",
        (f"-{args.days} days",),
    ).fetchall()

    # 2. Shorts recientes
    shorts = conn.execute(
        """SELECT id, channel_id, youtube_id, title, status
             FROM shorts
            WHERE youtube_id IS NOT NULL AND youtube_id != ''
              AND status IN ('published')
              AND date(published_at) >= date('now', 'localtime', ?)
            ORDER BY id DESC""",
        (f"-{args.days} days",),
    ).fetchall()
    conn.close()

    total = 0
    removed = 0
    for row in list(vids) + list(shorts):
        yt_id = (row["yt_video_id"] if "yt_video_id" in row.keys() else row["youtube_id"])
        if not yt_id:
            continue
        total += 1
        st = watch_page_status(yt_id)
        if st == "removed":
            removed += 1
            title = row["titulo_final"] if "titulo_final" in row.keys() else row["title"]
            channel_id = row["channel_id"]
            canal = row["canal"] if "canal" in row.keys() else db.get_channel(channel_id).get("slug", f"ch{channel_id}")
            logger.warning("REMOVED: %s (%s) '%s'", yt_id, canal, (title or "")[:70])
            if not args.dry_run and not _already_alerted(db, yt_id):
                try:
                    create_alert(
                        db,
                        entity_type="channel",
                        entity_id=channel_id,
                        channel_id=channel_id,
                        alert_type="silent_removal",
                        severity="warning",
                        title=f"Canal {canal}: vídeo eliminado silenciosamente por YouTube",
                        message=(
                            f"YouTube eliminó retroactivamente un vídeo que constaba como "
                            f"publicado: {yt_id}\nTítulo: {title}\n\n"
                            f"Esto es una señal de riesgo de spam/IA. No incrementar la "
                            f"frecuencia y revisar YouTube Studio antes de reincidir."
                        ),
                        metadata={"video_id": yt_id, "title": title, "channel": canal},
                    )
                    _mark_alerted(db, yt_id)
                except Exception as exc:
                    logger.warning("create_alert failed for %s: %s", yt_id, exc)
        time.sleep(0.4)  # cortesía, evita flood a YouTube

    logger.info("Sweep completado: %d revisados, %d eliminados por YouTube.", total, removed)


if __name__ == "__main__":
    main()
