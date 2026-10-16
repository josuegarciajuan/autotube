"""Perceptual comparison between a local thumbnail and YouTube's served one.

Why: ``thumbnails().set()`` can return HTTP 200 while YouTube silently keeps
its auto-generated frame (known flakiness, especially when the thumbnail is
set before the video finishes processing). The only reliable signal is to
compare the image YouTube actually serves against the local file.

This module downloads ``i.ytimg.com/vi/<id>/maxresdefault.jpg`` (0 API quota)
and compares a coarse grayscale grid against the local thumbnail.

Contract of :func:`thumbnails_match`:
    ``True``  → served image matches the local thumbnail (custom applied)
    ``False`` → served image differs (custom NOT applied)
    ``None``  → could not fetch/compare (private video, 404, bad file...)
"""

from __future__ import annotations

import io
import logging
import urllib.request
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

# Downsample both images to GRID x GRID grayscale before comparing.
GRID = 64
# Mean absolute per-pixel difference (0-255) below which we consider a match.
# Empirically: identical files → <1; different scenes → >25.
DEFAULT_THRESHOLD = 8.0
_UA = "Mozilla/5.0 (compatible; autotube-thumb-verify/1.0)"


def _to_grid(image) -> Optional[list[int]]:
    """Return the flattened GRID x GRID grayscale pixels of an image."""
    from PIL import Image  # local import: keeps module import cheap

    try:
        if isinstance(image, (str, Path)):
            image = Image.open(image)
        return list(image.convert("L").resize((GRID, GRID)).getdata())
    except Exception as exc:  # noqa: BLE001
        logger.debug("thumbnail_compare: cannot read image: %s", exc)
        return None


def fetch_served_thumbnail(video_id: str, timeout: int = 20):
    """Download the maxres thumbnail YouTube serves for ``video_id``.

    Returns a PIL Image or ``None`` if it cannot be fetched (404/private/network).
    """
    from PIL import Image  # noqa: F401 (ensures hard dependency is explicit)

    url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Cache-Control": "no-cache"},
        )
        data = urllib.request.urlopen(req, timeout=timeout).read()
        return Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        logger.debug("thumbnail_compare: fetch failed for %s: %s", video_id, exc)
        return None


def thumbnails_match(
    local_path: Union[str, Path],
    video_id: str,
    threshold: float = DEFAULT_THRESHOLD,
    timeout: int = 20,
    served_image=None,
) -> Optional[bool]:
    """Compare the local thumbnail with the one YouTube serves.

    Args:
        local_path: path to the locally generated thumbnail.
        video_id: YouTube video id (used to build the maxres URL).
        threshold: max mean absolute difference to still count as a match.
        timeout: HTTP timeout for the download.
        served_image: optional pre-fetched PIL image (avoids a second request).

    Returns:
        True (match), False (mismatch) or None (cannot determine).
    """
    local_px = _to_grid(local_path)
    if local_px is None:
        return None

    image = served_image if served_image is not None else fetch_served_thumbnail(video_id, timeout=timeout)
    if image is None:
        return None

    served_px = _to_grid(image)
    if served_px is None:
        return None

    diff = sum(abs(a - b) for a, b in zip(local_px, served_px)) / float(GRID * GRID)
    logger.debug(
        "thumbnail_compare: %s diff=%.2f threshold=%.2f", video_id, diff, threshold,
    )
    return diff < threshold
