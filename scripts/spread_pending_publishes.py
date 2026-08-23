#!/usr/bin/env python3
"""Esparcir publicaciones long-form pendientes a máx N/día por canal (antiban).

Contexto (ago 2026): tras strikes de spam, un canal puede acumular un backlog de
vídeos subidos como private con publishAt nativo (estado "calentando"). El
repack histórico usaba gaps de 3h → hasta 8 publicaciones/día del mismo canal,
la señal de ráfaga que YouTube penaliza. Este script reesparce TODAS las
publicaciones pendientes de un canal a gap_hours (default 24h = máx 1/día),
reprogramando el publishAt nativo en YouTube (videos.update, 50 ud) + DB
(videos.target_public_at, planned_slots, video_lifecycle_actions.go_public).

Reutiliza apply_publish_repack (api/services/publish_repack.py) con gap_hours
grande. Respeto de invariantes:
  - Saltar canales bloqueados por spam (sus publicaciones ya fueron retenidas).
  - Nunca publicar antes de now + warmup; el primer slot = siguiente pico del canal.
  - No tocar vídeos cuya hora ya sea correcta (sin llamadas de cuota inútiles).

Uso (desde /root/autotube o un worktree):
  python3 scripts/spread_pending_publishes.py --dry-run           # solo plan
  python3 scripts/spread_pending_publishes.py --apply             # aplicar a todos los canales con backlog
  python3 scripts/spread_pending_publishes.py --apply --channels 3,5
  python3 scripts/spread_pending_publishes.py --apply --gap-hours 48

Si se ejecuta desde un worktree, apuntar a los datos de producción:
  AUTOTUBE_ROOT=/root/autotube python3 scripts/spread_pending_publishes.py --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Root del proyecto (funciona igual en worktree que en producción)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("spread_pending_publishes")

# Canales objetivo por defecto: los que detectemos con >=2 publicaciones
# pendientes (backlog). Con --channels se fuerza una lista.
AUTO_MIN_PENDING = 2
# Gap por defecto: 24h = máx 1 publicación/día (techo antiban).
DEFAULT_GAP_HOURS = 24
# Horizonte de seguridad amplio: con gaps de 24h y backlogs densos el cap
# histórico de 120h recorta y vuelve a apilar vídeos. 720h = 30 días.
DEFAULT_SAFETY_HOURS = 720


def _patch_production_root() -> None:
    """Si AUTOTUBE_ROOT está definido (worktree), apuntar DB y tokens al árbol principal."""
    root = os.getenv("AUTOTUBE_ROOT")
    if not root:
        return
    root_path = Path(root).resolve()
    if not (root_path / "autotube.db").exists():
        logger.warning("AUTOTUBE_ROOT=%s no contiene autotube.db — se ignora", root)
        return
    from config import settings
    settings.PROJECT_ROOT = root_path
    settings.DATABASE_PATH = root_path / "autotube.db"
    settings.TOKENS_DIR = root_path / "tokens"
    logger.info("Datos de producción: %s", root_path)


def _channels_with_backlog(db) -> list[tuple[int, str]]:
    """(channel_id, slug) de canales con >= AUTO_MIN_PENDING publicaciones pendientes."""
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT v.channel_id, c.slug, COUNT(*) n
               FROM videos v JOIN channels c ON c.id = v.channel_id
               WHERE v.status IN ('uploaded_private','warming','scheduled')
                 AND v.publish_mode = 'scheduled'
                 AND v.target_public_at IS NOT NULL
                 AND v.published_at IS NULL
               GROUP BY v.channel_id, c.slug
               HAVING COUNT(*) >= ?""",
            (AUTO_MIN_PENDING,),
        ).fetchall()
    return [(int(r["channel_id"]), r["slug"]) for r in rows]


def _pending_per_channel(db, channel_id: int) -> int:
    with db._connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) n FROM videos
               WHERE channel_id = ? AND status IN ('uploaded_private','warming','scheduled')
                 AND publish_mode = 'scheduled' AND target_public_at IS NOT NULL
                 AND published_at IS NULL""",
            (channel_id,),
        ).fetchone()
    return int(row[0])


def main() -> int:
    ap = argparse.ArgumentParser(description="Esparcir publicaciones pendientes a máx N/día (antiban)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="solo calcular el plan, no escribir nada")
    g.add_argument("--apply", action="store_true", help="reprogramar en YouTube + DB")
    ap.add_argument("--channels", default="", help="CSV de channel_ids (default: todos con backlog)")
    ap.add_argument("--gap-hours", type=int, default=DEFAULT_GAP_HOURS, help=f"separación mínima entre publicaciones (default {DEFAULT_GAP_HOURS})")
    ap.add_argument("--safety-hours", type=int, default=DEFAULT_SAFETY_HOURS, help=f"horizonte máx de reprogramación en horas (default {DEFAULT_SAFETY_HOURS})")
    args = ap.parse_args()

    _patch_production_root()

    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()

    if args.channels.strip():
        target_ids = [int(x.strip()) for x in args.channels.split(",") if x.strip()]
        targets = []
        for cid in target_ids:
            ch = db.get_channel(cid)
            if not ch:
                logger.error("Canal #%s no existe — se omite", cid)
                continue
            targets.append((cid, ch.get("slug", f"canal{cid}")))
    else:
        targets = _channels_with_backlog(db)
        if not targets:
            logger.info("Sin canales con backlog (>=%d pendientes) — nada que hacer", AUTO_MIN_PENDING)
            return 0

    logger.info("Canales objetivo: %s", ", ".join(f"#{cid} {slug} ({_pending_per_channel(db, cid)} pend.)" for cid, slug in targets))

    from api.services.publish_repack import apply_publish_repack

    results = []
    for cid, slug in targets:
        try:
            res = apply_publish_repack(
                db, cid, slug,
                dry_run=args.dry_run,
                max_yt_updates=None,      # acción explícita: reprogramar todos
                quota_gate=False,         # manual; la cuota real es 100k ud/día por proyecto
                gap_hours=args.gap_hours,
                safety_ahead_hours=args.safety_hours,
            )
        except Exception as exc:
            logger.error("[%s] repack falló: %s", slug, exc)
            res = {"error": str(exc)}
        results.append({"channel": slug, "channel_id": cid, **res})
        if args.dry_run:
            # Mostrar el plan de forma compacta
            for d in (res.get("details") or []):
                flag = "✅" if not d.get("changed") else "🔁"
                print(
                    f"  {flag} #{d['video_id']:<5} {d['status']:<15} "
                    f"{str(d.get('old_target') or '')[:19]:<21} → {str(d.get('new_target') or '')[:19]}"
                )

    print(json.dumps(
        [{"channel": r["channel"], "channel_id": r["channel_id"],
          "total": r.get("total", 0), "rescheduled": r.get("rescheduled", 0),
          "no_change": r.get("no_change", 0), "yt_failed": r.get("yt_failed", 0),
          "quota_skipped": r.get("quota_skipped", 0),
          "skipped_spam": r.get("skipped_spam", 0),
          "error": r.get("error")} for r in results],
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
