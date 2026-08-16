#!/usr/bin/env python3
"""
YouTube channel wall scraper — quota-free public video detection.

Replaces the YouTube Data API check (`videos.list part=status`) that consumed
1 quota unit per verification, and the `videos.update` "force go_public" that
consumed 50 units.

Uses the channel's public RSS feed, which is free, requires no auth, and is
not subject to bot-detection. A video appearing in this feed is by definition
PUBLIC (the feed only lists public uploads). Unlisted/private videos never
appear, which is exactly what the warming → public transition check needs.

Feed URL:  https://www.youtube.com/feeds/videos.xml?channel_id=UCxxx
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

logger = logging.getLogger("autotube.wall_scraper")

# Namespace used by the YouTube RSS schema.
YT_NS = "http://www.youtube.com/xml/schemas/2015"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _parse_published_utc(raw: Optional[str]) -> Optional[str]:
    """Normalize an RFC3339/Atom timestamp to a UTC ISO string.

    Returns the original string if parsing fails (best-effort).
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return raw


def fetch_channel_public_video_ids(
    channel_id: str, timeout: int = 30
) -> Dict[str, str]:
    """Fetch the channel's recent public uploads from its RSS feed.

    Args:
        channel_id: YouTube channel ID (UCxxx). Must be non-empty.
        timeout: HTTP timeout in seconds.

    Returns:
        Mapping of {yt_video_id: published_iso_utc} for the public uploads
        currently listed in the feed (most recent ~15). Empty dict on any
        failure (network, parse, empty feed) — callers decide how to react.
    """
    if not channel_id:
        logger.warning("wall_scraper: empty channel_id — cannot scrape")
        return {}

    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "es-ES,es;q=0.9"},
        )
        if resp.status_code != 200:
            logger.warning(
                "wall_scraper: RSS HTTP %d for channel %s",
                resp.status_code, channel_id,
            )
            return {}
    except Exception as exc:
        logger.warning(
            "wall_scraper: RSS fetch error for channel %s: %s",
            channel_id, str(exc)[:200],
        )
        return {}

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.warning(
            "wall_scraper: RSS XML parse error for channel %s: %s",
            channel_id, str(exc)[:200],
        )
        return {}

    result: Dict[str, str] = {}
    ns = {"yt": YT_NS, "atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns):
        vid_el = entry.find("yt:videoId", ns)
        pub_el = entry.find("atom:published", ns)
        if vid_el is None or not (vid_el.text or "").strip():
            continue
        vid = vid_el.text.strip()
        result[vid] = _parse_published_utc(pub_el.text if pub_el is not None else None)

    return result


def is_video_public(channel_id: str, yt_video_id: str, timeout: int = 30) -> Optional[bool]:
    """Return True if the video appears in the channel's public RSS feed.

    Returns:
        True  → video is public (present in feed).
        False → feed fetched successfully but video NOT present (still private
                / unlisted / processing / deleted).
        None  → could not determine (fetch/parse error) — caller should retry.
    """
    if not channel_id or not yt_video_id:
        return None
    ids = fetch_channel_public_video_ids(channel_id, timeout=timeout)
    if not ids:
        # Empty dict is ambiguous: could be a real empty feed OR a fetch error.
        # fetch_channel_public_video_ids already logged the reason; treat as
        # indeterminate so the caller retries rather than falsely alerting.
        return None
    return yt_video_id in ids
