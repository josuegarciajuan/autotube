#!/usr/bin/env python3
"""
Recover YouTube thumbnails, channel banners, and avatars by downloading
the originals from YouTube.

Usage:
    python3 scripts/recover_thumbnails.py
"""

import argparse
import logging
import os
import pickle
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Setup ────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR = PROJECT_ROOT / "output"
THUMB_DIR = OUTPUT_DIR / "thumbnails"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("recover_thumbnails")

# ── Database ─────────────────────────────────────────────────────────

def get_db_path() -> Path:
    """Find the database file - check autotube.db in project root first."""
    candidates = [
        PROJECT_ROOT / "autotube.db",
        PROJECT_ROOT / "database" / "autotube.db",
        PROJECT_ROOT / "database" / "data.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("No database found")


def get_videos(db_path: Path, video_id: Optional[int] = None, slug: Optional[str] = None) -> list[tuple]:
    """Return uploaded videos: (id, yt_video_id, canal_slug, thumbnail_path). Accepts optional filters."""
    query = """
        SELECT v.id, v.yt_video_id, v.canal, v.thumbnail_path, v.channel_id
        FROM videos v
        WHERE v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
    """
    params = []

    if video_id is not None:
        query += " AND v.id = ?"
        params.append(video_id)

    if slug:
        query += " AND (v.canal = ? OR v.channel_id IN (SELECT id FROM channels WHERE slug = ?))"
        params.extend([slug, slug])

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    # Resolve canal slug from channel_id if canal is legacy
    conn = sqlite3.connect(str(db_path))
    channel_map = {}
    cur2 = conn.execute("SELECT id, slug FROM channels")
    for cid, _slug in cur2:
        channel_map[cid] = _slug
    conn.close()

    result = []
    for vid, yt_id, canal, thumb_path, channel_id in rows:
        resolved_slug = canal if canal else channel_map.get(channel_id, "unknown")
        result.append((vid, yt_id, resolved_slug, thumb_path))
    return result


def get_channels(db_path: Path) -> list[tuple]:
    """Return channels: (id, slug, name, yt_channel_id, banner_url, avatar_url)."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute("""
        SELECT id, slug, name, yt_channel_id, banner_url, avatar_url
        FROM channels
        WHERE yt_channel_id IS NOT NULL AND yt_channel_id != ''
          AND slug != 'test'
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


# ── Thumbnail Download (public CDN, no auth) ────────────────────────

THUMBNAIL_SIZES = [
    ("maxresdefault", "maxresdefault"),
    ("sddefault", "sddefault"),
    ("hqdefault", "hqdefault"),
    ("mqdefault", "mqdefault"),
    ("0", "0"),  # default
]

def download_video_thumbnail(video_id: str, yt_video_id: str) -> Optional[bytes]:
    """Download a video thumbnail from YouTube's public CDN.

    Tries resolutions from highest to lowest. Returns image bytes or None.
    """
    for size_name, size_param in THUMBNAIL_SIZES:
        url = f"https://i.ytimg.com/vi/{yt_video_id}/{size_param}.jpg"
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 1000:
                logger.info("  ✓ Downloaded thumbnail (%s) for video %s", size_name, video_id)
                return resp.content
            elif resp.status_code == 404:
                continue
            else:
                logger.debug("  Thumbnail %s returned status %d (len=%d)", size_name, resp.status_code, len(resp.content))
                continue
        except Exception as e:
            logger.warning("  ✗ Error downloading %s: %s", size_name, e)
            continue

    logger.warning("  ✗ No thumbnail found for video %s (yt_id=%s)", video_id, yt_video_id)
    return None


def download_video_thumbnail_oauth(video_id: int, yt_video_id: str, creds) -> Optional[bytes]:
    """Download a video thumbnail via YouTube Data API with OAuth.
    
    This works for private/unlisted videos where the CDN returns 404.
    Requires valid OAuth credentials for the channel that owns the video.
    """
    try:
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = service.videos().list(
            part="snippet",
            id=yt_video_id,
        ).execute()

        items = resp.get("items", [])
        if not items:
            logger.warning("  ✗ OAuth: video %s not found via API", yt_video_id)
            return None

        thumbnails = items[0].get("snippet", {}).get("thumbnails", {})

        # Try resolutions from highest to lowest
        for size in ["maxres", "standard", "high", "medium", "default"]:
            if size in thumbnails:
                url = thumbnails[size]["url"]
                logger.info("  OAuth: trying %s thumbnail...", size)
                thumb_resp = requests.get(url, timeout=30)
                if thumb_resp.status_code == 200 and len(thumb_resp.content) > 1000:
                    logger.info("  ✓ OAuth: downloaded thumbnail for video %s (%s)", video_id, size)
                    return thumb_resp.content
                logger.warning("  OAuth: %s download failed (status=%d, len=%d)", size, thumb_resp.status_code, len(thumb_resp.content))

        logger.warning("  ✗ OAuth: no usable thumbnail found for video %s", video_id)
        return None

    except HttpError as e:
        logger.error("  ✗ OAuth: YouTube API error for video %s: %s", video_id, e)
        return None
    except Exception as e:
        logger.error("  ✗ OAuth: unexpected error for video %s: %s", video_id, e)
        return None


def recover_video_thumbnails(videos: list[tuple], oauth_fallback_slug: Optional[str] = None) -> dict:
    """Download and save all video thumbnails.

    If `oauth_fallback_slug` is provided, OAuth credentials for that slug
    are loaded and used as fallback when CDN download fails (e.g. private videos).
    """
    results = {"ok": 0, "fail": 0, "skipped": 0}

    # Load OAuth creds once if fallback requested
    creds = None
    if oauth_fallback_slug:
        creds = load_creds(oauth_fallback_slug)
        if creds:
            logger.info("OAuth fallback enabled for slug=%s", oauth_fallback_slug)
        else:
            logger.warning("OAuth fallback requested but creds invalid for %s", oauth_fallback_slug)

    for video_id, yt_video_id, slug, thumb_path in videos:
        # Determine save path
        slug_dir = THUMB_DIR / slug
        slug_dir.mkdir(parents=True, exist_ok=True)

        save_path = slug_dir / f"thumb_{video_id}.jpg"

        # Check if already exists and is valid
        if save_path.exists() and save_path.stat().st_size > 1000:
            logger.info("  [skip] Already exists: %s (%d bytes)", save_path.name, save_path.stat().st_size)
            results["skipped"] += 1
            continue

        logger.info("Video %s (%s) [%s] → %s", video_id, slug, yt_video_id, save_path.name)

        img_data = download_video_thumbnail(video_id, yt_video_id)

        # Fallback: try OAuth if CDN failed and credentials are available
        if img_data is None and creds:
            logger.info("  CDN failed, trying OAuth fallback for video %s...", video_id)
            img_data = download_video_thumbnail_oauth(video_id, yt_video_id, creds)

        if img_data:
            with open(save_path, "wb") as f:
                f.write(img_data)
            logger.info("  Saved to %s", save_path)
            results["ok"] += 1
        else:
            results["fail"] += 1

    return results


# ── Channel Images (requires OAuth) ──────────────────────────────────

def load_creds(slug: str):
    """Load OAuth credentials for a channel."""
    token_path = PROJECT_ROOT / "tokens" / f"{slug}.pickle"
    if not token_path.exists():
        logger.warning("  No token for %s at %s", slug, token_path)
        return None

    try:
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    except Exception as e:
        logger.error("  Cannot load token for %s: %s", slug, e)
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)
            logger.debug("  Refreshed token for %s", slug)
        except Exception as e:
            logger.error("  Token refresh failed for %s: %s", slug, e)
            return None

    if not creds.valid:
        logger.error("  Token invalid for %s", slug)
        return None

    return creds


def download_channel_banner(slug: str, creds) -> Optional[bytes]:
    """Get channel banner URL via YouTube API and download it."""
    try:
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = service.channels().list(
            part="brandingSettings",
            mine=True,
        ).execute()

        items = resp.get("items", [])
        if not items:
            logger.warning("  No channel data returned for %s", slug)
            return None

        branding = items[0].get("brandingSettings", {})
        image = branding.get("image", {})
        banner_url = image.get("bannerExternalUrl")

        if not banner_url:
            logger.warning("  No bannerExternalUrl for %s", slug)
            return None

        # The URL from API has "=w2560-fcrop64=1,00005a57ffffa5a8-k-c0xffffffff-no-nd-rj" suffix
        # Modify to get uncropped full-resolution version
        banner_url = banner_url.split("=")[0] + "=w2560-h1440-l100-nd"
        logger.debug("  Banner URL: %s", banner_url[:80] + "...")

        resp = requests.get(banner_url, timeout=60)
        if resp.status_code == 200 and len(resp.content) > 5000:
            return resp.content
        else:
            logger.warning("  Banner download failed: status=%d, len=%d", resp.status_code, len(resp.content))
            # Try original URL as fallback
            if "=" in image.get("bannerExternalUrl", ""):
                resp2 = requests.get(image["bannerExternalUrl"], timeout=60)
                if resp2.status_code == 200 and len(resp2.content) > 5000:
                    return resp2.content
            return None

    except HttpError as e:
        logger.error("  YouTube API error for %s banner: %s", slug, e)
        return None
    except Exception as e:
        logger.error("  Error downloading banner for %s: %s", slug, e)
        return None


def download_channel_avatar(slug: str, creds) -> Optional[bytes]:
    """Get channel avatar URL via YouTube API and download it."""
    try:
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = service.channels().list(
            part="snippet",
            mine=True,
        ).execute()

        items = resp.get("items", [])
        if not items:
            logger.warning("  No channel data returned for %s", slug)
            return None

        snippet = items[0].get("snippet", {})
        thumbnails = snippet.get("thumbnails", {})

        # Try highest resolution first
        for size in ["high", "medium", "default"]:
            if size in thumbnails:
                avatar_url = thumbnails[size]["url"]
                # Modify URL to request 800x800
                if "=s" in avatar_url:
                    avatar_url = avatar_url.split("=")[0] + "=s800-c-k-c0x00ffffff00-no-rj"

                logger.debug("  Avatar URL (%s): %s", size, avatar_url[:80] + "...")
                resp = requests.get(avatar_url, timeout=30)
                if resp.status_code == 200 and len(resp.content) > 2000:
                    return resp.content

        logger.warning("  No suitable avatar found for %s", slug)
        return None

    except HttpError as e:
        logger.error("  YouTube API error for %s avatar: %s", slug, e)
        return None
    except Exception as e:
        logger.error("  Error downloading avatar for %s: %s", slug, e)
        return None


def recover_channel_images(channels: list[tuple]) -> dict:
    """Download channel banners and avatars using OAuth."""
    results = {"banner_ok": 0, "banner_fail": 0, "avatar_ok": 0, "avatar_fail": 0}

    for channel_id, slug, name, yt_channel_id, banner_url_db, avatar_url_db in channels:
        slug_dir = THUMB_DIR / slug
        slug_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Channel %s (%s)", slug, name)

        # ── Authenticate ──
        creds = load_creds(slug)
        if not creds:
            logger.warning("  Cannot authenticate for %s", slug)
            results["banner_fail"] += 1
            results["avatar_fail"] += 1
            continue

        # ── Banner ──
        banner_path = slug_dir / "banner.jpg"
        banner_url_db_path = f"/autotube/thumbnails/{slug}/banner.jpg"

        if banner_path.exists() and banner_path.stat().st_size > 5000:
            logger.info("  [skip] Banner already exists (%d bytes)", banner_path.stat().st_size)
            results["banner_ok"] += 1
        else:
            logger.info("  Downloading banner...")
            banner_data = download_channel_banner(slug, creds)
            if banner_data:
                with open(banner_path, "wb") as f:
                    f.write(banner_data)
                logger.info("  ✓ Banner saved (%d bytes)", len(banner_data))
                results["banner_ok"] += 1
            else:
                logger.warning("  ✗ Banner download failed")
                results["banner_fail"] += 1

        # ── Avatar ──
        avatar_path = slug_dir / "avatar.jpg"
        avatar_url_db_path = f"/autotube/thumbnails/{slug}/avatar.jpg"

        if avatar_path.exists() and avatar_path.stat().st_size > 2000:
            logger.info("  [skip] Avatar already exists (%d bytes)", avatar_path.stat().st_size)
            results["avatar_ok"] += 1
        else:
            logger.info("  Downloading avatar...")
            avatar_data = download_channel_avatar(slug, creds)
            if avatar_data:
                with open(avatar_path, "wb") as f:
                    f.write(avatar_data)
                logger.info("  ✓ Avatar saved (%d bytes)", len(avatar_data))
                results["avatar_ok"] += 1
            else:
                logger.warning("  ✗ Avatar download failed")
                results["avatar_fail"] += 1

    return results


# ── Copy from assets/ if YouTube download fails ─────────────────────

def copy_fallback_from_assets(channels: list[tuple]) -> dict:
    """If banner/avatar download from YouTube API failed, copy the static
    assets/ versions as fallback (these are the ones committed to git)."""
    results = {"banner_copied": 0, "avatar_copied": 0}

    for channel_id, slug, name, yt_channel_id, _, _ in channels:
        slug_dir = THUMB_DIR / slug

        # Copy banner from assets if missing
        banner_path = slug_dir / "banner.jpg"
        if not banner_path.exists() or banner_path.stat().st_size < 5000:
            asset_banner = PROJECT_ROOT / "assets" / slug / "banner.jpg"
            if asset_banner.exists():
                import shutil
                shutil.copy2(asset_banner, banner_path)
                logger.info("  ✓ Copied fallback banner from assets (%d bytes)", asset_banner.stat().st_size)
                results["banner_copied"] += 1

        # Copy avatar from assets if missing
        avatar_path = slug_dir / "avatar.jpg"
        if not avatar_path.exists() or avatar_path.stat().st_size < 2000:
            asset_avatar = PROJECT_ROOT / "assets" / slug / "avatar.png"
            if asset_avatar.exists():
                import shutil
                from PIL import Image
                img = Image.open(asset_avatar)
                # Convert from PNG to JPG
                img = img.convert("RGB")
                img.save(avatar_path, "JPEG", quality=95)
                logger.info("  ✓ Copied fallback avatar from assets (converted PNG→JPG, %d bytes)", avatar_path.stat().st_size)
                results["avatar_copied"] += 1

    return results


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Recover YouTube thumbnails, banners & avatars",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Recover all video thumbnails and channel images\n"
            "  python3 scripts/recover_thumbnails.py\n"
            "\n"
            "  # Recover thumbnail for a single video (with OAuth fallback for private videos)\n"
            "  python3 scripts/recover_thumbnails.py --video-id 57\n"
            "\n"
            "  # Recover thumbnail for a single video, narrow by channel slug\n"
            "  python3 scripts/recover_thumbnails.py --video-id 57 --slug canal2\n"
        ),
    )
    parser.add_argument(
        "--video-id", type=int, default=None,
        help="Recover thumbnail for a specific video ID (enables OAuth fallback for private videos)",
    )
    parser.add_argument(
        "--slug", type=str, default=None,
        help="Filter by channel slug (optional, auto-detected if --video-id is given without --slug)",
    )
    args = parser.parse_args()

    # Determine mode
    single_video_mode = args.video_id is not None

    if single_video_mode:
        logger.info("🎯 SINGLE VIDEO MODE (video_id=%d)", args.video_id)
    else:
        logger.info("Batch mode — recovering all video thumbnails")

    logger.info("=" * 60)
    logger.info("RECOVER YOUTUBE THUMBNAILS, BANNERS & AVATARS")
    logger.info("=" * 60)

    # ── Find database ──
    try:
        db_path = get_db_path()
        logger.info("Database: %s", db_path)
    except FileNotFoundError as e:
        logger.error("Cannot find database: %s", e)
        sys.exit(1)

    # ── Ensure output dirs exist ──
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Video thumbnails ──
    if single_video_mode:
        videos = get_videos(db_path, video_id=args.video_id, slug=args.slug)
    else:
        videos = get_videos(db_path)

    if not videos:
        logger.error("No matching videos found in database")
        sys.exit(1)

    logger.info("\n📹 STEP 1: Video thumbnails (public YouTube CDN)")
    logger.info("-" * 40)
    logger.info("Found %d video(s) to process", len(videos))

    # Enable OAuth fallback for single-video mode
    if single_video_mode:
        # Detect slug from the video record if not explicitly provided
        if args.slug:
            oauth_slug = args.slug
        elif videos:
            oauth_slug = videos[0][2]  # slug is 3rd element in tuple
        else:
            oauth_slug = None
        thumb_results = recover_video_thumbnails(videos, oauth_fallback_slug=oauth_slug)
        # Skip channel image steps in single-video mode
        logger.info("\n✅ Single video recovery complete.")
        if thumb_results["ok"] > 0:
            logger.info("Thumbnail recovered successfully for video %d", args.video_id)
        elif thumb_results["fail"] > 0:
            logger.error("Could not recover thumbnail for video %d — check YouTube API and OAuth token", args.video_id)
            sys.exit(1)
        return 0

    # ── Batch mode: full recovery ──
    thumb_results = recover_video_thumbnails(videos)

    # ── Step 2: Channel banners & avatars (OAuth) ──
    logger.info("\n🏷️  STEP 2: Channel banners & avatars (YouTube API)")
    logger.info("-" * 40)
    channels = get_channels(db_path)
    logger.info("Found %d channels with YouTube IDs", len(channels))

    channel_results = recover_channel_images(channels)

    # ── Step 3: Fallback from assets/ ──
    logger.info("\n🔄 STEP 3: Fallback from assets/ for any missing images")
    logger.info("-" * 40)
    fallback_results = copy_fallback_from_assets(channels)

    # ── Summary ──
    logger.info("\n" + "=" * 60)
    logger.info("RECOVERY SUMMARY")
    logger.info("=" * 60)
    logger.info("Video thumbnails:  %d ok, %d failed, %d skipped",
                thumb_results["ok"], thumb_results["fail"], thumb_results["skipped"])
    logger.info("Channel banners:   %d ok, %d failed, %d fallback",
                channel_results["banner_ok"], channel_results["banner_fail"], fallback_results["banner_copied"])
    logger.info("Channel avatars:   %d ok, %d failed, %d fallback",
                channel_results["avatar_ok"], channel_results["avatar_fail"], fallback_results["avatar_copied"])

    # ── Verify final state ──
    logger.info("\n📁 Final state of output/thumbnails/")
    for slug_dir in sorted(THUMB_DIR.iterdir()):
        if slug_dir.is_dir():
            files = sorted(slug_dir.iterdir())
            total_size = sum(f.stat().st_size for f in files if f.is_file())
            logger.info("  %s/ → %d files (%d KB)", slug_dir.name, len(files), total_size // 1024)
            for f in files:
                kb = f.stat().st_size // 1024
                logger.info("    %s (%d KB)", f.name, kb)

    total_ok = thumb_results["ok"] + channel_results["banner_ok"] + channel_results["avatar_ok"]
    total_fallback = fallback_results["banner_copied"] + fallback_results["avatar_copied"]
    total_fail = thumb_results["fail"] + channel_results["banner_fail"] + channel_results["avatar_fail"]

    if total_fail == 0:
        logger.info("\n✅ All images recovered successfully!")
    else:
        logger.warning("\n⚠️  %d images could not be downloaded from YouTube API", total_fail)
        if total_fallback > 0:
            logger.info("   %d were recovered from static assets/ as fallback", total_fallback)

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
