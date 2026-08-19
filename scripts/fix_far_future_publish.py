#!/usr/bin/env python3
"""Reprogramar el publishAt de vídeos en "calentando" (uploaded_private) a días vista.

Problema: los vídeos subidos como private heredan el target_public_at del slot
planificado (horizonte de planning 7d + lead boost) y se quedan "calentando"
hasta 12 días sin publicar.

Este script:
1. Escanea vídeos con status IN ('uploaded_private','warming','scheduled') cuyo
   target_public_at está a más de MAX_PUBLISH_AT_AHEAD_HOURS (24h) en el futuro.
2. Recalcula el target al siguiente pico (vía clamp_max_ahead_target_public_at).
3. Reprograma el publishAt en YouTube vía videos().update(part='status') — solo
   funciona en vídeos private, que es exactamente el caso.
4. Si YouTube acepta → actualiza DB (videos.target_public_at,
   planned_slots.target_public_at, video_lifecycle_actions.scheduled_for de
   go_public pendientes) y registra en scheduled_publish_logger.
5. Si YouTube falla (quota/auth/HTTP) → se deja como está (publicará solo a la
   hora original) y se avisa. Nunca se fuerza una publicación manual.

Safe to re-run: los targets ya dentro de 24h se saltan.
"""

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Ensure project root in path ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("fix_far_future_publish")

DB_PATH = "/root/autotube/autotube.db"


def _get_channel_cfg(conn, channel_id: int) -> dict:
    """Channel config parsed from config_json + slug."""
    row = conn.execute(
        "SELECT slug, config_json FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    if not row:
        return {}
    cfg = {}
    try:
        cfg = json.loads(row["config_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    cfg["slug"] = row["slug"]
    return cfg


def find_far_future_videos(conn, max_ahead_hours: int = 24) -> list[dict]:
    """Vídeos en calentando con target_public_at > now + max_ahead_hours.

    Incluye una tolerancia de 30 min para no re-reportar vídeos ya clampados a
    ~24h (el cap es now+24h y 'now' avanza entre pasadas).
    """
    from pipeline.publish_scheduler import _parse_target_public_at

    rows = conn.execute(
        """SELECT v.id, v.channel_id, v.canal, v.yt_video_id, v.target_public_at,
                  v.titulo_final, c.slug as slug
           FROM videos v JOIN channels c ON c.id = v.channel_id
           WHERE v.status IN ('uploaded_private','warming','scheduled')
             AND v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
             AND v.target_public_at IS NOT NULL
           ORDER BY v.target_public_at
        """
    ).fetchall()

    now_utc = datetime.now(timezone.utc)
    tolerance = timedelta(minutes=30)
    far = []
    for row in rows:
        slug = row["slug"] or row["canal"]
        cfg = _get_channel_cfg(conn, row["channel_id"])
        tz_str = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
        parsed = _parse_target_public_at(str(row["target_public_at"]), tz_str)
        if parsed is None:
            continue
        ahead_hours = (parsed - now_utc).total_seconds() / 3600
        if parsed > now_utc + timedelta(hours=max_ahead_hours) + tolerance:
            far.append({
                "id": row["id"],
                "channel_id": row["channel_id"],
                "slug": slug,
                "yt_video_id": row["yt_video_id"],
                "target_public_at": str(row["target_public_at"]),
                "ahead_hours": round(ahead_hours, 1),
                "titulo": row["titulo_final"],
            })
    return far


def remediate(conn, video: dict, dry_run: bool = False) -> dict:
    """Recalcula el target y (si no es dry-run) lo reprograma en YouTube."""
    from pipeline.publish_scheduler import clamp_max_ahead_target_public_at

    channel_id = video["channel_id"]
    slug = video["slug"]
    cfg = _get_channel_cfg(conn, channel_id)
    tz_str = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
    warmup = int(cfg.get("PUBLISH_WARMUP_MIN", 120) or 120)
    primary_kw = cfg.get("SEO_PRIMARY_KEYWORD", "")
    secondary_kws = cfg.get("SEO_SECONDARY_KEYWORDS", [])
    target_h = cfg.get("PUBLISH_TARGET_HOUR")

    old_target = video["target_public_at"]

    # ── 1. Calcular nuevo target (siguiente pico, <=24h) ──
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        new_target = clamp_max_ahead_target_public_at(
            old_target, slug=slug, timezone_str=tz_str, warmup_min=warmup,
            db=db, channel_id=channel_id,
            primary_keyword=primary_kw,
            secondary_keywords=secondary_kws,
            target_hour=target_h,
        )
    except Exception as exc:
        logger.error("  FAIL #%d [%s]: clamp error: %s", video["id"], slug, exc)
        return {"status": "error", "error": str(exc)}

    if not new_target or new_target == old_target:
        logger.info("  #%d [%s] target ya <=24h (%s) — sin cambio",
                    video["id"], slug, str(old_target)[:19])
        return {"status": "noop", "new_target": new_target}

    logger.info(
        "  #%d [%s] '%s...' → %s (era %s, %.0fh) | yt=%s",
        video["id"], slug, (video["titulo"] or "?")[:30],
        str(new_target)[:19], str(old_target)[:19], video["ahead_hours"],
        video["yt_video_id"],
    )

    if dry_run:
        return {"status": "would_fix", "new_target": new_target}

    # ── 2. Reprogramar publishAt en YouTube ──
    try:
        from pipeline.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(slug)
        if not uploader.authenticate():
            raise RuntimeError("auth fallida")
        result = uploader.set_publish_at(video["yt_video_id"], new_target)
        if not result.get("updated"):
            raise RuntimeError(f"respuesta inesperada: {result}")
    except Exception as exc:
        logger.warning(
            "  ⚠️ #%d [%s] YouTube rechazó reprogramar (%s) — se deja como está, "
            "publicará solo a la hora original %s",
            video["id"], slug, exc, str(old_target)[:19],
        )
        return {"status": "yt_failed", "error": str(exc), "new_target": new_target}

    # ── 3. Actualizar DB (target + dependencias) ──
    try:
        conn.execute(
            "UPDATE videos SET target_public_at = ? WHERE id = ?",
            (new_target, video["id"]),
        )
        conn.execute(
            "UPDATE planned_slots SET target_public_at = ? WHERE video_id = ?",
            (new_target, video["id"]),
        )
        conn.execute(
            """UPDATE video_lifecycle_actions SET scheduled_for = ?
               WHERE video_id = ? AND action_type = 'go_public' AND status = 'pending'""",
            (new_target, video["id"]),
        )
        conn.commit()
    except Exception as exc:
        logger.error("  FAIL #%d: DB update error: %s", video["id"], exc)
        return {"status": "db_error", "error": str(exc), "new_target": new_target}

    # ── 4. Log en scheduled_publish_logger ──
    try:
        from api.services.scheduled_publish_logger import log_publish_event
        log_publish_event(
            event="rescheduled",
            slug=slug,
            video_title=(video["titulo"] or "?")[:80],
            yt_video_id=video["yt_video_id"],
            db_video_id=video["id"],
            uploaded_at=None,
            target_public_at=str(old_target),
            actual_public_at=new_target,
            local_time=f"(clamp 24h: {old_target[:19]} → {new_target[:19]})",
        )
    except Exception:
        pass

    return {"status": "fixed", "new_target": new_target}


def main():
    parser = argparse.ArgumentParser(
        description="Reprogramar publishAt de vídeos 'calentando' a días vista"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar qué se haría sin tocar YouTube ni la DB")
    parser.add_argument("--db", type=str, default=DB_PATH,
                        help="Ruta a la base de datos SQLite")
    parser.add_argument("--max-ahead-hours", type=int, default=24,
                        help="Antelación máxima aceptable (default 24)")
    args = parser.parse_args()

    if not Path(args.db).exists():
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    try:
        videos = find_far_future_videos(conn, args.max_ahead_hours)
        if not videos:
            logger.info("No hay vídeos 'calentando' a más de %dh — todo OK",
                        args.max_ahead_hours)
            return

        logger.info("Encontrados %d vídeo(s) a más de %dh vista:",
                    len(videos), args.max_ahead_hours)
        for v in videos:
            logger.info(
                "  #%d [%s] %.0fh | %s | %s",
                v["id"], v["slug"], v["ahead_hours"],
                str(v["target_public_at"])[:19],
                (v["titulo"] or "?")[:50],
            )

        mode = "DRY RUN (no changes)" if args.dry_run else "REPROGRAMANDO"
        logger.info("\n%s %d vídeo(s)...", mode, len(videos))

        results = {"fixed": 0, "would_fix": 0, "noop": 0, "yt_failed": 0,
                   "db_error": 0, "error": 0}
        for v in videos:
            r = remediate(conn, v, dry_run=args.dry_run)
            status = r.get("status")
            results[status if status in results else "error"] += 1

        logger.info(
            "\nResultado: %s",
            {k: v for k, v in results.items() if v > 0},
        )
        if results.get("yt_failed"):
            logger.info(
                "Los %d rechazados por YouTube publicarán solos a su hora original; "
                "para adelantarlos a mano: YouTube Studio → vídeo → programar.",
                results["yt_failed"],
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
