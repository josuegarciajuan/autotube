#!/usr/bin/env python3
"""Push corrected Spanish descriptions from local DB to YouTube for viral-mode
videos whose current YT description still contains English leaks (Discord,
Patreon, @handles, URLs from the original creator, etc.).

Modes:
  --push-only    Find affected videos (clean in DB, leaked on YT) and push the
                 DB description to YouTube. Includes random human-like pauses.
  --dry-run      Preview only — no pushes, no quota consumption.

Quota cost: 1 unit (videos.list per video) + 50 units (videos.update per push).
"""

import argparse
import json
import logging
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fix_viral_descriptions")

# ── Leak patterns (same as viral_cloner) ──────────────────────
_LEAK_PATTERNS = [
    (r'https?://[^\s]+', 'URL'),
    (r'discord\.gg/', 'Discord'),
    (r'(?:patreon|paypal|buymeacoffee)\.', 'payment'),
    (r'bit\.ly/', 'short link'),
    (r'@[a-zA-Z0-9_]{3,}', '@handle'),
    (r'(?:subscribe|suscribete|suscríbete)', 'subscribe CTA', re.IGNORECASE),
    (r'(?:activate|hit|press)\s*(?:the\s+)?(?:bell|notification)', 'bell CTA', re.IGNORECASE),
    (r'(?:join|follow|find)\s+(?:me|us|my|our)\s+(?:on|at)', 'follow CTA', re.IGNORECASE),
]


def description_has_leaks(desc: str) -> bool:
    """Check if a description contains foreign promo content."""
    if not desc or len(desc) < 30:
        return False
    if desc[:20].lower().startswith(("join my discord", "watch ad-free", "directed by @")):
        return True
    for pattern_data in _LEAK_PATTERNS:
        if isinstance(pattern_data, tuple):
            pat, _, *rest = pattern_data
            flags = rest[0] if rest else 0
            if re.search(pat, desc, flags=flags):
                return True
    return False


def get_affected_videos(conn: sqlite3.Connection) -> list[dict]:
    """Find viral videos that have a clean DB description + valid yt_video_id."""
    rows = conn.execute("""
        SELECT v.id, v.canal, v.titulo_final, v.description as db_desc,
               v.yt_video_id, v.status
        FROM videos v
        WHERE v.source_mode = 'viral'
          AND v.yt_video_id IS NOT NULL
          AND v.yt_video_id != ''
          AND v.description IS NOT NULL
          AND v.description != ''
          AND NOT (v.description LIKE '%http%' OR v.description LIKE '%discord%'
                   OR v.description LIKE '%patreon%'
                   OR v.description LIKE '%subscribe%')
        ORDER BY v.canal, v.id
    """).fetchall()
    return [dict(r) for r in rows]


def get_youtube_service(channel_slug: str):
    """Get an authenticated YouTube service for a channel."""
    try:
        from pipeline.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(
            account_name=channel_slug,
            channel_slug=channel_slug,
        )
        if not uploader.authenticate():
            logger.error("[%s] ✗ Auth failed", channel_slug)
            return None, None
        service = uploader._get_service()
        logger.info("[%s] ✓ Authenticated", channel_slug)
        return service, uploader
    except Exception as e:
        logger.error("[%s] ✗ Failed to init: %s", channel_slug, e)
        return None, None


def fetch_yt_description(service, yt_video_id: str) -> str | None:
    """Fetch the current description from YouTube. Returns None on failure."""
    try:
        resp = service.videos().list(
            part="snippet",
            id=yt_video_id,
            maxResults=1,
        ).execute()
        items = resp.get("items", [])
        if not items:
            logger.warning("  Video %s not found on YouTube", yt_video_id)
            return None
        return items[0]["snippet"].get("description", "")
    except Exception as e:
        logger.warning("  Fetch failed for %s: %s", yt_video_id, e)
        return None


def push_description_to_youtube(service, yt_video_id: str, new_desc: str) -> bool:
    """Push a new description to YouTube preserving title/category."""
    try:
        # Fetch current snippet to preserve title + categoryId
        current = service.videos().list(
            part="snippet",
            id=yt_video_id,
            maxResults=1,
        ).execute()
        if not current.get("items"):
            return False
        snippet = current["items"][0]["snippet"]
        body = {
            "id": yt_video_id,
            "snippet": {
                "title": snippet["title"],
                "categoryId": snippet["categoryId"],
                "description": new_desc[:5000],
            },
        }
        service.videos().update(part="snippet", body=body).execute()
        return True
    except Exception as e:
        logger.error("  Push failed: %s", e)
        return False


def verify_push(service, yt_video_id: str, expected_first_chars: str) -> bool:
    """Verify the pushed description matches what we sent (check first 60 chars)."""
    try:
        resp = service.videos().list(
            part="snippet",
            id=yt_video_id,
            maxResults=1,
        ).execute()
        items = resp.get("items", [])
        if not items:
            return False
        current = items[0]["snippet"].get("description", "")
        current_clean = current.replace("\n", " ").replace("\r", " ")[:60].strip()
        expected_clean = expected_first_chars.replace("\n", " ").replace("\r", "  ")[:60].strip()
        # Fuzzy match — first 40 chars should be almost identical
        match_len = min(40, len(current_clean), len(expected_clean))
        if match_len < 10:
            return False
        return current_clean[:match_len] == expected_clean[:match_len]
    except Exception as e:
        logger.warning("  Verify failed: %s", e)
        return False


def human_pause(seconds_range: tuple, label: str = ""):
    """Sleep for a random time in [min, max] seconds. Logs the pause."""
    delay = random.uniform(*seconds_range)
    mins = int(delay // 60)
    secs = int(delay % 60)
    if mins:
        label_str = f" ({label})" if label else ""
        logger.info("  ⏸ Pausa humana%s: %dm %ds...", label_str, mins, secs)
    else:
        label_str = f" ({label})" if label else ""
        logger.info("  ⏸ Pausa humana%s: %ds...", label_str, secs)
    time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description="Push fixed descriptions to YouTube")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--push-only", action="store_true",
                        help="Push clean DB descriptions to YouTube (with human pauses)")
    group.add_argument("--dry-run", action="store_true",
                        help="Preview only — no pushes")
    parser.add_argument("--min-pause", type=int, default=45,
                        help="Min seconds between videos (default: 45)")
    parser.add_argument("--max-pause", type=int, default=150,
                        help="Max seconds between videos (default: 150)")
    parser.add_argument("--channel-gap-min", type=int, default=120,
                        help="Extra min pause between channels (default: 120)")
    parser.add_argument("--channel-gap-max", type=int, default=240,
                        help="Extra max pause between channels (default: 240)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip post-push verification")
    args = parser.parse_args()

    db_path = PROJECT_ROOT / "autotube.db"
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # ── Find candidates ──────────────────────────────────────
    all_videos = get_affected_videos(conn)

    if not all_videos:
        print("No viral videos with clean DB descriptions found.")
        conn.close()
        return

    # ── Check which ones still have leaks on YouTube ─────────
    need_push = []

    # Group by channel for efficient auth
    channels = sorted(set(v["canal"] for v in all_videos))
    for canal in channels:
        logger.info("[%s] Authenticating...", canal)
        service, uploader = get_youtube_service(canal)
        if not service:
            logger.warning("[%s] ⚠ Skipping all videos for this channel (auth failed)", canal)
            continue

        channel_videos = [v for v in all_videos if v["canal"] == canal]
        logger.info("[%s] Checking %d videos on YouTube...", canal, len(channel_videos))

        for vid in channel_videos:
            yt_id = vid["yt_video_id"]
            yt_desc = fetch_yt_description(service, yt_id)
            if yt_desc is None:
                continue  # video not found — skip

            has_leaks = description_has_leaks(yt_desc)
            db_desc = vid["db_desc"]

            if has_leaks:
                logger.info("  video #%d (%s): YT=%d chars LEAKED → pushing DB version (%d chars)",
                            vid["id"], yt_id, len(yt_desc), len(db_desc))
                need_push.append({**vid, "service": service, "yt_desc_snippet": yt_desc[:100]})
            elif yt_desc[:40] != db_desc[:40]:
                # Not leaked but different — could be stale
                logger.info("  video #%d (%s): YT is outdated (different text) → updating",
                            vid["id"], yt_id)
                need_push.append({**vid, "service": service, "yt_desc_snippet": yt_desc[:100]})
            else:
                logger.debug("  video #%d (%s): already up to date ✓", vid["id"], yt_id)

    conn.close()

    if not need_push:
        print("\nAll videos already have correct descriptions on YouTube.")
        return

    print(f"\n{'═' * 60}")
    print(f"  Videos to push:  {len(need_push)}")
    print(f"  Channels:         {len(set(v['canal'] for v in need_push))}")
    print(f"{'═' * 60}")

    if args.dry_run:
        print("\n── DRY RUN — no pushes made. Use --push-only to execute. ──")
        for vid in need_push:
            print(f"  [{vid['canal']}] video #{vid['id']} \"{vid['titulo_final'][:60]}\" "
                  f"({vid['yt_video_id']}) — YT has: {vid['yt_desc_snippet']}")
        return

    # ── Push with human pauses ───────────────────────────────
    results = []
    prev_canal = None
    push_count = 0
    verify_ok = 0
    verify_fail = 0
    verify_skip = 0

    print(f"\n{'═' * 60}")
    print(f"  Starting pushes with human-like pauses")
    print(f"  Between videos: {args.min_pause}s – {args.max_pause}s")
    print(f"  Between channels: {args.channel_gap_min}s – {args.channel_gap_max}s")
    print(f"{'═' * 60}\n")

    for i, vid in enumerate(need_push):
        canal = vid["canal"]

        # ── Extra pause when switching channels ──
        if prev_canal and prev_canal != canal:
            human_pause(
                (args.channel_gap_min, args.channel_gap_max),
                f"cambio de canal: {prev_canal} → {canal}",
            )
        elif prev_canal:
            # ── Standard pause between videos in same channel ──
            human_pause((args.min_pause, args.max_pause))

        prev_canal = canal

        yt_id = vid["yt_video_id"]
        db_desc = vid["db_desc"]
        service = vid["service"]

        logger.info("[%s] Pushing video #%d (%s)...", canal, vid["id"], yt_id)
        ok = push_description_to_youtube(service, yt_id, db_desc)
        push_count += 1

        if ok:
            # ── Verify the push ──
            if not args.no_verify:
                # Small delay to let YouTube propagate
                time.sleep(2.0)
                verified = verify_push(service, yt_id, db_desc)
                if verified:
                    verify_ok += 1
                    logger.info("  ✅ Verified: description updated on YouTube")
                else:
                    verify_fail += 1
                    logger.warning("  ⚠ Push reported OK but verification failed — may need manual check")
            else:
                verify_skip += 1

            results.append({
                "id": vid["id"],
                "canal": canal,
                "titulo": vid["titulo_final"][:70],
                "yt_id": yt_id,
                "status": "pushed" if ok else "failed",
                "verified": True if (args.no_verify or verified) else False,
            })
            logger.info("  ✅ Pushed! (%d/%d)", push_count, len(need_push))
        else:
            results.append({
                "id": vid["id"],
                "canal": canal,
                "titulo": vid["titulo_final"][:70],
                "yt_id": yt_id,
                "status": "failed",
                "verified": False,
            })
            logger.error("  ❌ Push failed for video #%d", vid["id"])

    # ── Final report ──────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print(f"  REPORTE FINAL")
    print(f"{'═' * 60}\n")

    succeeded = [r for r in results if r["status"] == "pushed"]
    failed = [r for r in results if r["status"] != "pushed"]

    if succeeded:
        print("✅ Actualizados correctamente:\n")
        for r in succeeded:
            verif = "✓ verif" if r["verified"] else "⚠ sin verif"
            print(f"   [{r['canal']}] video #{r['id']} | {r['yt_id']}")
            print(f"      Título: {r['titulo']}")
            print(f"      Estado: actualizado ({verif})\n")

    if failed:
        print("❌ Fallaron:\n")
        for r in failed:
            print(f"   [{r['canal']}] video #{r['id']} | {r['yt_id']}")
            print(f"      Título: {r['titulo']}")
            print(f"      Estado: FALLÓ\n")

    print(f"{'═' * 60}")
    print(f"  Total:        {len(results)}")
    print(f"  Push OK:      {len(succeeded)}")
    print(f"  Verificado:   {verify_ok}")
    print(f"  Pend. verif:  {verify_fail + verify_skip}")
    print(f"  Falló:        {len(failed)}")
    print(f"{'═' * 60}")


if __name__ == "__main__":
    main()
