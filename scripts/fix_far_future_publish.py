#!/usr/bin/env python3
"""Reprogramar publicaciones de vídeos "calentando"/pendientes con gaps >=3h.

Problema: el clamp con cap duro apilaba muchos vídeos del mismo canal en el
mismo instante (p. ej. 8 a la misma hora) cuando el backlog era denso, y la
resolución incremental de colisiones dejaba huecos <3h.

Este script recalcula TODAS las publicaciones pendientes del canal en una sola
pasada (repack_channel_publish_times) con separación >= 3h y:
1. Reprograma en YouTube (videos().update status.publishAt) los vídeos ya
   subidos como private.
2. Actualiza DB (target_public_at, planned_slots, go_public) y el
   scheduled_upload_at de los listos para subir para que la subida quepa
   antes de la publicación (publicación = después de subida + warmup).
3. Registra cada cambio en scheduled_publish_logger (event "rescheduled").

Si YouTube rechaza (quota/auth/HTTP) se deja como está: el vídeo publicará
solo a su hora original. Safe to re-run: los ya bien espaciados se saltan.
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

# ── Ensure project root in path ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("fix_far_future_publish")

from config.settings import DATABASE_PATH
DB_PATH = DATABASE_PATH


def main():
    parser = argparse.ArgumentParser(
        description="Reprogramar publicaciones pendientes con gaps >=3h (repack)"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar el plan sin tocar YouTube ni la DB")
    parser.add_argument("--db", type=str, default=DB_PATH,
                        help="Ruta a la base de datos SQLite")
    parser.add_argument("--channel-id", type=int, default=None,
                        help="Reprogramar solo este canal (default: todos los afectados)")
    parser.add_argument("--force-yt", action="store_true",
                        help="Resincronizar YouTube aunque la DB ya coincida con el plan "
                             "(útil tras desincronización por límite de updates)")
    args = parser.parse_args()

    if not Path(args.db).exists():
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    import os
    os.environ["DATABASE_PATH"] = args.db
    from database.db_extended import ExtendedDatabase
    from api.services.publish_repack import apply_publish_repack

    db = ExtendedDatabase(args.db)

    if args.channel_id is not None:
        affected = [args.channel_id]
    else:
        # Canales con publicaciones pendientes lejanas o con huecos <3h
        from api.services.upload_scheduler import _channels_need_repack
        from datetime import datetime, timezone
        affected = _channels_need_repack(db, datetime.now(timezone.utc))
        if not affected:
            logger.info("No hay canales con publicaciones pendientes problemáticas — todo OK")
            return

    logger.info("Canales a reprogramar: %s", affected)
    totals = {"rescheduled": 0, "no_change": 0, "yt_failed": 0}
    for ch_id in affected:
        try:
            ch = db.get_channel(ch_id)
            slug = ch["slug"] if ch else f"canal{ch_id}"
        except Exception:
            slug = f"canal{ch_id}"
        logger.info("── Canal %s (id=%d) %s ──", slug, ch_id,
                    "(DRY RUN)" if args.dry_run else "")
        res = apply_publish_repack(
            db, ch_id, slug,
            dry_run=args.dry_run,
            max_yt_updates=None,
            quota_gate=False,
            force_yt=args.force_yt,
        )
        for k in totals:
            totals[k] += res.get(k, 0)
        logger.info(
            "  %s: %d reprogramados · %d sin cambio · %d rechazados por YT",
            slug, res.get("rescheduled", 0), res.get("no_change", 0),
            res.get("yt_failed", 0),
        )

    logger.info("\nTOTAL: %s", totals)
    if totals["yt_failed"] and not args.dry_run:
        logger.info(
            "Los rechazados por YouTube publicarán solos a su hora original; "
            "para adelantarlos a mano: YouTube Studio → vídeo → programar."
        )


if __name__ == "__main__":
    main()
