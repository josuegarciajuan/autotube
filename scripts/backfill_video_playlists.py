#!/usr/bin/env python3
"""Backfill: classify and assign existing videos to playlists (DB + YouTube).

Filters: long-form only (>60s), already uploaded (yt_video_id IS NOT NULL),
          not yet assigned (target_playlist_id IS NULL).

Usage:
    python3 scripts/backfill_video_playlists.py                # dry-run all
    python3 scripts/backfill_video_playlists.py --slug canal5  # dry-run one
    python3 scripts/backfill_video_playlists.py --slug canal5 --live  # execute
"""

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DATABASE_PATH, LLM_MODEL, LLM_API_KEY, LLM_BASE_URL
from database.db_extended import ExtendedDatabase, migrate_v2
from database.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_playlists")

_SKIP_CHANNEL_IDS = {6}


def _classify_video(title: str, description: str, playlists: list[dict]) -> str | None:
    """Use LLM to pick the best playlist for a video. Returns slug or None."""
    from openai import OpenAI

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    pl_desc = "\n".join(
        f"- {p['slug']}: {p['name']}"
        for p in playlists
    )

    system = (
        "Eres un clasificador experto de contenido de YouTube. "
        "Lee el título y descripción de un vídeo y elige la lista de reproducción "
        "que mejor encaje temáticamente. Responde SOLO con el slug exacto de la "
        "playlist elegida, sin comillas ni explicaciones."
    )

    user = (
        f"TÍTULO: {title[:200]}\n\n"
        f"DESCRIPCIÓN: {description[:500] or '(sin descripción)'}\n\n"
        f"PLAYLISTS DISPONIBLES:\n{pl_desc}\n\n"
        f"Elige el slug exacto de la playlist que mejor encaje."
    )

    from config.llm_helpers import llm_json_call  # not needed but import for consistency
    import time as _time

    last_exc = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.3,
                max_tokens=50,
            )
            result = resp.choices[0].message.content.strip().lower()
            if result:
                valid = {p["slug"].lower() for p in playlists}
                if result in valid:
                    for p in playlists:
                        if p["slug"].lower() == result:
                            return p["slug"]
                return None
            raise ValueError("LLM returned empty content")
        except Exception as e:
            last_exc = e
            if attempt < 2:
                delay = 2.0 * (2 ** attempt)
                logger.warning(
                    "LLM classification attempt %d/3 failed: %s — retrying in %.1fs",
                    attempt + 1, e, delay,
                )
                _time.sleep(delay)

    logger.warning("LLM classification failed after 3 retries: %s", last_exc)
    return None


def main():
    parser = argparse.ArgumentParser(description="Backfill video playlists")
    parser.add_argument("--live", action="store_true", help="Actually execute (default: dry-run)")
    parser.add_argument("--slug", type=str, default=None, help="Process only one channel")
    args = parser.parse_args()

    init_db()
    migrate_v2()
    db = ExtendedDatabase()

    if args.slug:
        ch = db.get_channel_by_slug(args.slug)
        channels = [ch] if ch else []
        if not channels:
            logger.error("Channel '%s' not found", args.slug)
            sys.exit(1)
    else:
        channels = db.get_channels(active_only=True)

    channels = [c for c in channels if c["id"] not in _SKIP_CHANNEL_IDS]
    mode = "LIVE" if args.live else "DRY-RUN"
    logger.info("Mode: %s | %d channel(s)", mode, len(channels))

    global_processed = 0
    global_assigned = 0
    global_errors = 0

    for ch in channels:
        channel_id = ch["id"]
        slug = ch["slug"]
        name = ch["name"]

        # Load playlists for this channel
        playlists = db.get_channel_youtube_playlists(channel_id)
        if not playlists:
            logger.info("[%s] No playlists — skipping", slug)
            continue

        # Find long-form videos without playlist assignment
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT id, titulo_final, description, yt_video_id, duracion_seg
                   FROM videos
                   WHERE channel_id = ?
                     AND yt_video_id IS NOT NULL
                     AND target_playlist_id IS NULL
                     AND status != 'short'
                     AND (duracion_seg > 60 OR duracion_seg IS NULL)
                   ORDER BY id""",
                (channel_id,),
            ).fetchall()
        videos = [dict(r) for r in rows]

        if not videos:
            logger.info("[%s] No pending videos", slug)
            continue

        logger.info("%s %s — %d video(s) pendientes %s", "=" * 30, name, len(videos), "=" * 30)

        for vid in videos:
            title = (vid.get("titulo_final") or "Sin título")[:80]
            desc = vid.get("description") or ""

            # Classify
            best_slug = _classify_video(title, desc, playlists)
            if not best_slug:
                logger.warning("  ❌ No se pudo clasificar: %s", title)
                global_errors += 1
                continue

            # Find playlist info
            pl = next((p for p in playlists if p["slug"] == best_slug), None)
            if not pl:
                logger.warning("  ❌ Slug '%s' no encontrado en playlists del canal", best_slug)
                global_errors += 1
                continue

            if args.live:
                # 1. Update DB
                db.update_video(vid["id"],
                                target_playlist_id=pl["id"],
                                target_playlist_slug=best_slug)
                # 2. Add to YouTube
                yt_result = None
                try:
                    from pipeline.youtube_playlists import YouTubePlaylistManager
                    mgr = YouTubePlaylistManager(slug)
                    mgr.authenticate()
                    yt_result = mgr.add_video_to_playlist(
                        pl["yt_playlist_id"], vid["yt_video_id"]
                    )
                except Exception as exc:
                    logger.error("  ⚠️ YouTube API error: %s", exc)

                yt_ok = yt_result and (yt_result.get("yt_playlist_item_id") or yt_result.get("was_already_present"))
                if yt_ok:
                    logger.info("  ✅ %s → %s", title, pl["name"])
                else:
                    logger.warning("  ⚠️ %s → %s (DB ok, YT: %s)", title, pl["name"],
                                   yt_result.get("error", yt_result) if yt_result else "no result")
                global_assigned += 1
            else:
                logger.info("  🔍 %s → %s", title, pl["name"])
                global_assigned += 1

            global_processed += 1

    logger.info("%s Resumen %s", "=" * 20, "=" * 20)
    logger.info("Procesados: %d | Asignados: %d | Errores: %d | Modo: %s",
               global_processed, global_assigned, global_errors, mode)
    if not args.live:
        logger.info("🔍 DRY-RUN — usa --live para ejecutar los cambios")


if __name__ == "__main__":
    main()
