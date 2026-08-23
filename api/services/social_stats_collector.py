"""Social stats collector — per-platform metrics with FREE APIs (0 YouTube quota).

Collects views/likes/comments/reposts for every published platform_video:
- Dailymotion: GET /v2/videos/{id}?fields=views_total,ratings_total,comments_total
- Bluesky:    com.atproto.identity.resolveHandle + app.bsky.feed.getPosts (public)
- Mastodon:   GET /api/v1/statuses/{id} (public)
- Facebook:   GET /{video_id}?fields=views,comments.limit(0).summary(true) (page token)
- Rumble:     scrape público de la watch page (0 quota, best-effort)

Snapshots go to platform_video_stats (time series) + platform_videos snapshot cols.
"""

import logging
import re

import requests

logger = logging.getLogger(__name__)


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ── Per-platform fetchers ──────────────────────────────────────


def _fetch_dailymotion(db, row: dict) -> dict:
    """Use the Dailymotion publisher's token (client_credentials)."""
    from api.services.publishers.base import get_publisher

    pid = row.get("platform_video_id") or ""
    if not pid:
        return {}
    try:
        pub = get_publisher("dailymotion")
        pub._authenticate(row.get("channel_id") or 0)
        return pub.get_stats(pid)  # async — handled by caller
    except Exception as exc:
        logger.warning("[Stats] Dailymotion fetch error: %s", exc)
    return {}


def _fetch_facebook(db, row: dict) -> dict:
    from pipeline.social_encryption import get_encryption

    pid = row.get("platform_video_id") or ""
    channel_id = row.get("channel_id")
    if not pid or not channel_id:
        return {}
    try:
        acct = db.get_social_account(channel_id, "facebook")
        if not acct or not acct.get("enabled"):
            return {}
        enc = get_encryption()
        tok = enc.decrypt(acct.get("encrypted_password", ""))
        if not tok:
            return {}
        resp = requests.get(
            f"https://graph.facebook.com/v18.0/{pid}",
            params={"access_token": tok, "fields": "views,comments.limit(0).summary(true)"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            comments = (data.get("comments") or {}).get("summary", {}).get("total_count", 0)
            return {
                "views": _safe_int(data.get("views")),
                "comments": _safe_int(comments),
            }
    except Exception as exc:
        logger.warning("[Stats] Facebook fetch error: %s", exc)
    return {}


def _fetch_bluesky(db, row: dict) -> dict:
    """Resolve handle → DID → getPosts (public, no auth)."""
    post_url = row.get("platform_video_url") or ""
    rkey = row.get("platform_video_id") or ""
    if not post_url and not rkey:
        return {}
    # https://bsky.app/profile/{handle}/post/{rkey}
    m = re.search(r"/profile/([^/]+)/post/([^/]+)", post_url)
    if not m:
        if not rkey:
            return {}
        return {}  # no handle stored — cannot resolve
    handle, rkey = m.group(1), m.group(2)
    try:
        r = requests.get(
            "https://bsky.social/xrpc/com.atproto.identity.resolveHandle",
            params={"handle": handle}, timeout=15,
        )
        if r.status_code != 200:
            return {}
        did = r.json().get("did", "")
        if not did:
            return {}
        uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        r2 = requests.post(
            "https://bsky.social/xrpc/app.bsky.feed.getPosts",
            json={"uris": [uri]}, timeout=15,
        )
        if r2.status_code == 200:
            posts = (r2.json() or {}).get("posts", [])
            if posts:
                p = posts[0]
                return {
                    "likes": _safe_int(p.get("likeCount")),
                    "reposts": _safe_int(p.get("repostCount")),
                    "comments": _safe_int(p.get("replyCount")),
                }
    except Exception as exc:
        logger.warning("[Stats] Bluesky fetch error: %s", exc)
    return {}


def _fetch_mastodon(db, row: dict) -> dict:
    """Public status endpoint — no auth needed for public posts."""
    post_url = row.get("platform_video_url") or ""
    status_id = row.get("platform_video_id") or ""
    if not post_url or not status_id:
        return {}
    m = re.search(r"https://([^/]+)/", post_url)
    if not m:
        return {}
    instance = m.group(1)
    try:
        r = requests.get(
            f"https://{instance}/api/v1/statuses/{status_id}",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return {
                "likes": _safe_int(data.get("favourites_count")),
                "reposts": _safe_int(data.get("reblogs_count")),
                "comments": _safe_int(data.get("replies_count")),
            }
    except Exception as exc:
        logger.warning("[Stats] Mastodon fetch error: %s", exc)
    return {}


def _fetch_rumble(db, row: dict) -> dict:
    """Scrape the public Rumble watch page (0 quota, best-effort)."""
    post_url = row.get("platform_video_url") or ""
    if not post_url:
        return {}
    try:
        r = requests.get(post_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code == 200:
            m = re.search(r"([\d.,]+)\s*views?", r.text[:200000], re.IGNORECASE)
            if m:
                views = m.group(1).replace(",", "").replace(".", "")
                return {"views": _safe_int(views)}
    except Exception as exc:
        logger.warning("[Stats] Rumble fetch error: %s", exc)
    return {}


_FETCHERS = {
    "dailymotion": _fetch_dailymotion,
    "facebook": _fetch_facebook,
    "bluesky": _fetch_bluesky,
    "mastodon": _fetch_mastodon,
    "rumble": _fetch_rumble,
}


# ── Orchestrator ───────────────────────────────────────────────


def collect_channel_stats(db, channel_id: int = None) -> dict:
    """Collect stats for all published platform videos (one channel or all).

    Returns {platform: {checked, updated, errors}} per platform.
    """
    import asyncio

    results: dict = {}
    rows = db.get_published_platform_videos(channel_id)
    if not rows:
        return results

    for platform in sorted({r["platform"] for r in rows}):
        fetcher = _FETCHERS.get(platform)
        stats = {"checked": 0, "updated": 0, "errors": 0}
        for row in rows:
            if row["platform"] != platform:
                continue
            stats["checked"] += 1
            try:
                if platform == "dailymotion":
                    # get_stats is async — run it
                    from api.services.publishers.base import get_publisher
                    pub = get_publisher("dailymotion")
                    pub._authenticate(row.get("channel_id") or 0)
                    data = asyncio.run(pub.get_stats(row.get("platform_video_id") or ""))
                else:
                    data = fetcher(db, row)
                if data:
                    db.record_platform_video_stats(
                        row["id"], platform,
                        views=data.get("views", 0),
                        likes=data.get("likes", 0),
                        comments=data.get("comments", 0),
                        reposts=data.get("reposts", 0),
                    )
                    stats["updated"] += 1
            except Exception as exc:
                stats["errors"] += 1
                logger.warning("[Stats] %s row %s failed: %s", platform, row.get("id"), exc)
        results[platform] = stats

    return results
