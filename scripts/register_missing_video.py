#!/usr/bin/env python3
"""Register a YouTube video in the local DB that was uploaded but not saved.

Usage:
    python3 scripts/register_missing_video.py aTSywxpLiRA canal3
    python3 scripts/register_missing_video.py --help

For canal3 (Civilizaciones Olvidadas), the missing video from 2026-07-10:
    youtube_id = aTSywxpLiRA
    title      = Bill Brown: El misterio oculto del rock and roll
"""

import argparse
import json
import logging
import os
import pickle
import sqlite3
import sys
from pathlib import Path

# -- add project root to sys.path so we can import config + DB --
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger("register_missing")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
DATABASE_PATH = PROJECT_ROOT / "autotube.db"


def load_oauth_token(slug: str):
    """Load or refresh an OAuth pickle token for the given channel slug."""
    token_path = PROJECT_ROOT / "tokens" / f"{slug}.pickle"
    secret_path = PROJECT_ROOT / "config" / f"client_secret_{slug}.json"
    if not secret_path.exists():
        secret_path = PROJECT_ROOT / "config" / "client_secret.json"

    if not secret_path.exists():
        raise FileNotFoundError(
            f"No client_secret found for {slug} at {secret_path} or fallback"
        )

    credentials = None
    if token_path.exists():
        with open(token_path, "rb") as f:
            credentials = pickle.load(f)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Refreshing expired token for %s…", slug)
            credentials.refresh(Request())
            with open(token_path, "wb") as f:
                pickle.dump(credentials, f)
        else:
            raise RuntimeError(
                f"Token for {slug} is invalid/expired and cannot be refreshed. "
                f"Re-authenticate with: python3 scripts/oauth_quick.py"
            )

    return credentials


def fetch_video_metadata(youtube_id: str, slug: str) -> dict:
    """Fetch snippet + contentDetails from YouTube Data API v3."""
    creds = load_oauth_token(slug)
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)

    resp = (
        service.videos()
        .list(part="snippet,contentDetails,statistics", id=youtube_id)
        .execute()
    )

    items = resp.get("items", [])
    if not items:
        raise ValueError(f"Video {youtube_id} not found via YouTube API (channel: {slug})")

    item = items[0]
    snippet = item.get("snippet", {})
    content_details = item.get("contentDetails", {})
    stats = item.get("statistics", {})

    # Parse ISO 8601 duration → seconds
    duration_str = content_details.get("duration", "PT0S")  # e.g. "PT8M30S"
    duracion_seg = _parse_duration(duration_str)

    thumbnails = snippet.get("thumbnails", {})
    thumb_url = (
        thumbnails.get("maxres", {}).get("url")
        or thumbnails.get("high", {}).get("url")
        or thumbnails.get("medium", {}).get("url")
        or ""
    )

    published_at = snippet.get("publishedAt", "")

    return {
        "youtube_id": youtube_id,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "tags": snippet.get("tags", []),
        "duracion_seg": duracion_seg,
        "thumbnail_url": thumb_url,
        "published_at": published_at,
        "category_id": snippet.get("categoryId", "22"),
        "privacy_status": "public",  # guess; we don't have status API
        "view_count": int(stats.get("viewCount", 0)),
        "like_count": int(stats.get("likeCount", 0)),
        "comment_count": int(stats.get("commentCount", 0)),
    }


def _parse_duration(iso_duration: str) -> int:
    """Convert ISO 8601 duration (e.g. PT8M30S) to seconds."""
    import re

    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not match:
        return 0
    h, m, s = match.groups()
    return int(h or 0) * 3600 + int(m or 0) * 60 + int(s or 0)


def insert_video_to_db(meta: dict, channel_id: int, canal_slug: str):
    """Insert a video record into the local SQLite DB."""
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    tags_json = json.dumps(meta["tags"], ensure_ascii=False) if meta["tags"] else None
    yt_url = f"https://youtube.com/watch?v={meta['youtube_id']}"

    # Check for existing record to avoid duplicate
    existing = conn.execute(
        "SELECT id FROM videos WHERE yt_video_id = ?", (meta["youtube_id"],)
    ).fetchone()
    if existing:
        logger.warning(
            "Video %s already exists in DB (video_id=%d) — skipping insert",
            meta["youtube_id"],
            existing["id"],
        )
        conn.close()
        return existing["id"]

    cursor = conn.execute(
        """INSERT INTO videos (
            canal, channel_id, video_path, audio_path,
            yt_video_id, yt_url, titulo_final, description,
            duracion_seg, privacy_status, tags_json,
            status, progress, progress_phase,
            uploaded_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            canal_slug,
            channel_id,
            "",  # video_path — not available locally
            "",  # audio_path — not available locally
            meta["youtube_id"],
            yt_url,
            meta["title"],
            meta["description"],
            meta["duracion_seg"],
            meta["privacy_status"],
            tags_json,
            "uploaded",
            100,
            "upload",
            meta["published_at"],
            meta["published_at"],
        ),
    )
    conn.commit()
    video_id = cursor.lastrowid
    conn.close()
    logger.info("Inserted video_id=%d with yt_video_id=%s", video_id, meta["youtube_id"])
    return video_id


def insert_stats_snapshot(video_id: int, meta: dict):
    """Insert initial stats snapshot into video_stats_history."""
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.execute("PRAGMA busy_timeout = 30000")

    now = meta["published_at"]

    conn.execute(
        """INSERT OR IGNORE INTO video_stats_history
           (video_id, yt_video_id, fetched_at,
            views, likes, comments)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            video_id,
            meta["youtube_id"],
            now,
            meta["view_count"],
            meta["like_count"],
            meta["comment_count"],
        ),
    )
    conn.commit()
    conn.close()
    logger.info(
        "Stats snapshot saved: views=%d likes=%d comments=%d",
        meta["view_count"],
        meta["like_count"],
        meta["comment_count"],
    )


def main():
    parser = argparse.ArgumentParser(
        description="Register a YT-uploaded video in the local DB"
    )
    parser.add_argument("youtube_id", help="YouTube video ID (e.g. aTSywxpLiRA)")
    parser.add_argument(
        "slug",
        help="Channel slug",
    )
    parser.add_argument(
        "--channel-id",
        type=int,
        default=None,
        help="Override channel DB id (auto-detected from channels table)",
    )
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help="Skip inserting stats snapshot",
    )
    args = parser.parse_args()

    # Resolve channel_id from DB
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    if args.channel_id:
        channel_id = args.channel_id
    else:
        row = conn.execute(
            "SELECT id FROM channels WHERE slug = ?", (args.slug,)
        ).fetchone()
        if not row:
            logger.error("Channel slug '%s' not found in DB", args.slug)
            sys.exit(1)
        channel_id = row["id"]
    conn.close()

    logger.info("Fetching metadata for %s (channel: %s, channel_id=%d)…",
                args.youtube_id, args.slug, channel_id)

    try:
        meta = fetch_video_metadata(args.youtube_id, args.slug)
    except Exception as exc:
        logger.error("Failed to fetch video metadata: %s", exc)
        sys.exit(1)

    logger.info("Title: %s", meta["title"])
    logger.info("Duration: %ds  Tags: %d  Views: %d",
                meta["duracion_seg"], len(meta["tags"]), meta["view_count"])

    video_id = insert_video_to_db(meta, channel_id, args.slug)

    if not args.no_stats:
        insert_stats_snapshot(video_id, meta)

    logger.info("Done! Video %s registered as DB id=%d", args.youtube_id, video_id)


if __name__ == "__main__":
    main()
