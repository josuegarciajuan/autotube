#!/usr/bin/env python3
"""End-to-end social media publishing test script.

Tests the complete social publishing pipeline for an existing video:
1. Loads channel + video data from DB or from a video ID
2. Generates platform-optimized caption (LLM)
3. Generates vertical clip if needed (TikTok/Instagram)
4. Publishes to the social platform via browser automation
5. Reports result

Usage:
    # Dry run (preview only — no actual publishing)
    python3 scripts/test_social.py --canal canal2 --platform twitter --video-id 123 --dry-run

    # Real publish
    python3 scripts/test_social.py --canal canal2 --platform twitter --video-id 123 --real

    # Test all platforms for a video
    python3 scripts/test_social.py --canal canal2 --video-id 123 --all --dry-run

    # Test with a specific caption (skip LLM generation)
    python3 scripts/test_social.py --canal canal2 --platform twitter --video-id 123 \\
        --caption "This is my test tweet" --real

    # List available videos for a channel
    python3 scripts/test_social.py --canal canal2 --list-videos
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("test_social")


async def test_social_publish(
    channel_id: int,
    channel_slug: str,
    platform: str,
    video_id: int,
    yt_video_id: str,
    video_title: str,
    yt_url: str,
    script_text: str,
    dry_run: bool = False,
    manual_caption: str = None,
    headless: bool = True,
) -> dict:
    """Run the full social publishing pipeline for a video."""
    from pipeline.social_caption_generator import SocialCaptionGenerator
    from pipeline.social_clip_extractor import SocialClipExtractor
    from pipeline.social_browser import BrowserSessionManager
    from pipeline.social_publishers.base import SocialContent, get_publisher
    from pipeline.social_encryption import get_encryption
    from database.db_extended import ExtendedDatabase

    db = ExtendedDatabase()
    result = {
        "ok": False,
        "platform": platform,
        "post_url": "",
        "caption": "",
        "clip_path": "",
        "error": "",
    }

    # ── 1. Get credentials ──
    acct = db.get_social_account(channel_id, platform)
    if not acct:
        logger.error("No %s account configured for %s", platform, channel_slug)
        result["error"] = f"No {platform} account configured"
        return result

    enc = get_encryption()
    password = enc.decrypt(acct["encrypted_password"])
    if not password:
        logger.error("Cannot decrypt %s password", platform)
        result["error"] = "Cannot decrypt password"
        return result

    logger.info("👤 Account: %s on %s", acct["username"], platform)

    # ── 2. Generate caption ──
    caption_data = None
    if manual_caption:
        caption = manual_caption
        logger.info("📝 Using manual caption: %s", caption[:80])
    else:
        cap_gen = SocialCaptionGenerator()
        channel_niche = ""
        try:
            from config.config_bridge import get_channel_config
            config = get_channel_config(channel_slug)
            channel_niche = getattr(config, "SEO_PRIMARY_KEYWORD", "")
        except Exception:
            pass

        caption_data = cap_gen.generate(
            platform=platform,
            script_text=script_text,
            video_title=video_title,
            yt_url=yt_url,
            channel_niche=channel_niche,
        )
        caption = caption_data.text
        result["caption"] = caption
        logger.info("📝 Generated caption (%d chars): %s...", len(caption), caption[:100])

    # ── 3. Generate clip if needed ──
    clip_path = ""
    if platform in ("tiktok", "instagram"):
        logger.info("🎬 Generating vertical clip for %s...", platform)
        try:
            from config.settings import OUTPUT_DIR

            # Find video file
            video_path = None
            candidates = list(Path(OUTPUT_DIR / "videos" / channel_slug).glob(f"*{video_id}*"))
            if candidates:
                video_path = str(candidates[0])
            else:
                # Search by yt_video_id
                candidates2 = list(Path(OUTPUT_DIR / "videos" / channel_slug).glob(f"*{yt_video_id}*"))
                if candidates2:
                    video_path = str(candidates2[0])

            if not video_path:
                # Try the videos table file_path
                video = db.get_video(video_id)
                if video and video.get("file_path"):
                    video_path = video["file_path"]

            if video_path and os.path.exists(video_path):
                logger.info("   Source video: %s", video_path)

                # Get duration
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                     "-of", "csv=p=0", video_path],
                    capture_output=True, text=True, timeout=10,
                )
                total_dur = float(probe.stdout.strip() or 120)

                extractor = SocialClipExtractor()
                clip_output = OUTPUT_DIR / "social_clips" / channel_slug
                os.makedirs(clip_output, exist_ok=True)
                clip_path = str(clip_output / f"test_{platform}_{video_id}.mp4")

                # Extract middle 55s
                start = max(0, (total_dur / 2) - 27.5)
                duration = min(55, total_dur - start)

                extractor.extract_clip(
                    video_path, start, duration, clip_path,
                    subtitle_text=caption[:200],
                )
                result["clip_path"] = clip_path
                logger.info("   Clip saved: %s (%.1fs from %.1fs)", clip_path, duration, start)
            else:
                logger.warning("   No video file found — skipping clip generation")
        except Exception as exc:
            logger.warning("   Clip generation failed (non-fatal): %s", exc)
    else:
        logger.info("📄 Text-only platform — no clip needed")

    # ── 4. Determine link strategy ──
    link_strategy_map = {
        "tiktok": "bio",
        "twitter": "last_tweet",
        "instagram": "bio",
        "facebook": "direct",
        "reddit": "none",
    }
    link_strategy = link_strategy_map.get(platform, "none")

    # ── 5. Build content ──
    publisher = get_publisher(platform)
    content = SocialContent(
        platform=platform,
        text=caption,
        media_path=clip_path,
        yt_url=yt_url,
        thread_parts=caption_data.thread_parts if not manual_caption else [],
        hashtags=caption_data.hashtags if not manual_caption else [],
        link_strategy=link_strategy,
    )

    # ── 6. Publish ──
    mode = "DRY-RUN" if dry_run else "LIVE"
    logger.info("🚀 Publishing to %s [%s]...", platform, mode)

    try:
        async with BrowserSessionManager(headless=headless) as bsm:
            page = await bsm.new_page()

            # Load cookies
            if acct.get("cookies_json"):
                await bsm.load_cookies(page, acct["cookies_json"])

            if dry_run:
                logger.info("   DRY-RUN: Would publish:")
                logger.info("   Caption: %s...", caption[:80])
                if clip_path:
                    logger.info("   Clip: %s", clip_path)
                logger.info("   Link strategy: %s", link_strategy)

                # Take a screenshot of the current page
                screenshot_dir = OUTPUT_DIR / "social_tests"
                os.makedirs(screenshot_dir, exist_ok=True)
                screenshot_path = str(screenshot_dir / f"dryrun_{platform}_{video_id}.png")
                await page.screenshot(path=screenshot_path)
                logger.info("   Screenshot saved: %s", screenshot_path)

                result["ok"] = True
                result["post_url"] = f"[DRY-RUN] {screenshot_path}"
                return result

            # Real publish
            post_url = await publisher.publish(page, content)

            if post_url:
                # Save updated cookies
                new_cookies = await bsm.save_cookies(page)
                if new_cookies:
                    db.update_social_cookies(acct["id"], new_cookies)

                # Create log entry
                db.create_social_post_log(
                    video_id=video_id, channel_id=channel_id, platform=platform,
                    account_id=acct["id"], caption_text=caption[:2000],
                    status="published",
                )

                logger.info("✅ Published! URL: %s", post_url)
                result["ok"] = True
                result["post_url"] = post_url
            else:
                logger.error("❌ Publish returned no URL — check platform")
                result["error"] = "publish returned no URL"

    except Exception as exc:
        logger.error("❌ Browser error: %s", exc)
        result["error"] = str(exc)

    return result


async def test_all_platforms(
    channel_id: int, channel_slug: str, video_id: int,
    yt_video_id: str, video_title: str, yt_url: str, script_text: str,
    dry_run: bool = False, headless: bool = True,
) -> list[dict]:
    """Test publishing to all configured platforms."""
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    accounts = db.get_enabled_social_accounts(channel_id)

    if not accounts:
        logger.error("No enabled social accounts for %s", channel_slug)
        return []

    results = []
    for acct in accounts:
        platform = acct["platform"]
        logger.info("\n%s Testing %s... %s", "=" * 40, platform, "=" * 40)
        r = await test_social_publish(
            channel_id, channel_slug, platform, video_id,
            yt_video_id, video_title, yt_url, script_text,
            dry_run=dry_run, headless=headless,
        )
        results.append(r)
        logger.info("   Result: %s", "✅ OK" if r["ok"] else f"❌ {r['error']}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="End-to-end social media publishing test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run (preview only)
  python3 scripts/test_social.py --canal canal2 --platform twitter --video-id 123 --dry-run

  # Real publish
  python3 scripts/test_social.py --canal canal2 --platform twitter --video-id 123 --real

  # Test all platforms
  python3 scripts/test_social.py --canal canal2 --video-id 123 --all --dry-run

  # Manual caption
  python3 scripts/test_social.py --canal canal2 --platform twitter --video-id 123 \\
      --caption "Test tweet" --real

  # List videos
  python3 scripts/test_social.py --canal canal2 --list-videos
        """,
    )
    parser.add_argument("--canal", required=True, help="Channel slug")
    parser.add_argument("--platform", help="Platform to test")
    parser.add_argument("--all", action="store_true", help="Test all enabled platforms")
    parser.add_argument("--video-id", type=int, help="DB video ID")
    parser.add_argument("--list-videos", action="store_true", help="List available videos")
    parser.add_argument("--dry-run", action="store_true", help="Preview without actually posting")
    parser.add_argument("--real", action="store_true", help="Actually publish (DANGER ZONE)")
    parser.add_argument("--caption", help="Manual caption (skip LLM generation)")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run browser in headless mode (default)")
    parser.add_argument("--visible", action="store_true", help="Show browser window (for debugging)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Get channel ──
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    ch = db.get_channel_by_slug(args.canal)
    if not ch:
        logger.error("Channel not found: %s", args.canal)
        sys.exit(1)

    channel_id = ch["id"]
    channel_slug = ch["slug"]
    logger.info("Channel: %s (id=%d)", channel_slug, channel_id)

    # ── List videos ──
    if args.list_videos:
        videos = db.get_channel_videos(channel_id, status="uploaded")
        uploaded = [v for v in videos if v.get("yt_video_id")]
        logger.info("Videos with YT ID for %s:", channel_slug)
        for v in uploaded[:20]:
            logger.info("  id=%d | %s | %s | yt=%s",
                        v["id"], v.get("titulo_final", "N/A")[:50], v.get("status"), v.get("yt_video_id", "N/A"))
        if not uploaded:
            logger.info("  No uploaded videos with yt_video_id. Try status='ready' or 'uploaded'")
        return

    if not args.video_id:
        parser.error("--video-id required (use --list-videos to find one)")

    # ── Validate mode ──
    if not args.dry_run and not args.real:
        parser.error("Specify --dry-run or --real")
    if args.dry_run and args.real:
        parser.error("Choose one: --dry-run or --real")

    headless = not args.visible

    # ── Get video ──
    video = db.get_video(args.video_id)
    if not video:
        logger.error("Video %d not found", args.video_id)
        sys.exit(1)

    yt_video_id = video.get("yt_video_id", "")
    video_title = video.get("titulo_final", "") or video.get("title", f"Video {args.video_id}")
    yt_url = f"https://youtu.be/{yt_video_id}" if yt_video_id else ""

    # Get script text
    script_text = ""
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT text FROM scripts WHERE video_id = ? ORDER BY id DESC LIMIT 1",
                (args.video_id,),
            ).fetchone()
            if row:
                script_text = row["text"] or ""
    except Exception:
        pass
    if not script_text:
        script_text = video.get("title_options", "") or video_title

    logger.info("Video: %s (id=%d, yt=%s)", video_title[:50], args.video_id, yt_video_id)
    logger.info("Script: %d chars", len(script_text))

    # ── Run test ──
    if args.all:
        results = asyncio.run(test_all_platforms(
            channel_id, channel_slug, args.video_id,
            yt_video_id, video_title, yt_url, script_text,
            dry_run=args.dry_run, headless=headless,
        ))
        ok = sum(1 for r in results if r["ok"])
        logger.info("\n%s", "=" * 50)
        logger.info("✅ %d/%d platforms OK", ok, len(results))
        logger.info("=" * 50)
    else:
        if not args.platform:
            parser.error("--platform required (or use --all)")
        result = asyncio.run(test_social_publish(
            channel_id, channel_slug, args.platform.lower(),
            args.video_id, yt_video_id, video_title, yt_url, script_text,
            dry_run=args.dry_run, manual_caption=args.caption, headless=headless,
        ))
        logger.info("\n%s", "=" * 50)
        logger.info("Result: %s", "✅ OK" if result["ok"] else f"❌ {result['error']}")
        if result["post_url"]:
            logger.info("URL: %s", result["post_url"])
        if result["clip_path"]:
            logger.info("Clip: %s", result["clip_path"])
        logger.info("=" * 50)


if __name__ == "__main__":
    main()
