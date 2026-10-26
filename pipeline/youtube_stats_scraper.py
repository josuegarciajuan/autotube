"""Public YouTube stats scraper — zero Data API quota.

Retrieves public watch-page and channel metrics (viewCount, likeCount,
commentCount, subscriberCount) via yt-dlp (innertube web client).

Used as a RESILIENCE fallback in stats collection: when the YouTube Data API
v3 daily quota is exhausted, the "Recolectar stats" button still refreshes
public metrics instead of failing or zeroing out likes/comments.

It consumes NO YouTube Data API units — only public HTTP requests. It can even
run for channels without an OAuth token (public data needs no auth).
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as _FuturesTimeout

logger = logging.getLogger(__name__)

# Silence yt-dlp's stderr "ERROR: [youtube] ..." noise for unavailable videos
# (we already handle failures ourselves via is_mock=True).
_ytdlp_logger = logging.getLogger("autotube.ytdlp_null")
_ytdlp_logger.addHandler(logging.NullHandler())
_ytdlp_logger.propagate = False
_ytdlp_logger.setLevel(logging.CRITICAL)

_WATCH_URL = "https://www.youtube.com/watch?v={id}"


def _parse_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_timeout(name: str, default: int) -> int:
    """Entero de env con suelo de 10 s."""
    try:
        return max(10, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# ── Timeouts de yt-dlp (oct 2026, fase anti-cuelgues) ───────────
# Antes: ydl_opts sin socket_timeout → un socket muerto (o un bot-check lento)
# colgaba el extractor indefinidamente Y `as_completed` esperaba sin límite.
# Ahora cada conexión acota su socket y los reintentos están limitados; el
# límite duro final es el proceso del recolector.
YTDLP_SOCKET_TIMEOUT_S = _env_timeout("YTDLP_SOCKET_TIMEOUT_S", 30)


def _ydl_base_opts() -> dict:
    """Opciones comunes: socket acotado + reintentos limitados."""
    return {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_S,
        "retries": 1,            # reconexiones HTTP (red)
        "extractor_retries": 1,  # reintentos del extractor
    }


class YouTubeStatsScraper:
    """Scrape public YouTube stats without the Data API (no quota)."""

    def __init__(self, channel_slug: str, max_concurrency: int = 6):
        self.slug = channel_slug
        self.max_concurrency = max(1, int(max_concurrency))

    # ── Video stats ──────────────────────────────────────────

    def _scrape_one_video(self, yt_id: str) -> dict:
        import yt_dlp

        ydl_opts = {
            **_ydl_base_opts(),
            "noplaylist": True,
            "skip_download": True,
            "ignoreerrors": True,
            "logger": _ytdlp_logger,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    _WATCH_URL.format(id=yt_id), download=False
                )
        except Exception as exc:
            logger.debug("[%s] scrape failed for %s: %s", self.slug, yt_id, exc)
            info = None

        if not info:
            return {
                "viewCount": "0",
                "likeCount": "0",
                "commentCount": "0",
                "is_mock": True,
            }

        return {
            "viewCount": str(_parse_int(info.get("view_count"))),
            "likeCount": str(_parse_int(info.get("like_count"))),
            "commentCount": str(_parse_int(info.get("comment_count"))),
            "is_mock": False,
        }

    def get_video_stats_batch(self, yt_video_ids: list[str], deadline: float = None) -> dict[str, dict]:
        """Scrape public stats for multiple videos in parallel.

        Args:
            yt_video_ids: video IDs a scrapear.
            deadline: epoch opcional — si expira, deja de esperar el resto,
                cancela los futures aún no arrancados y los marca como no
                disponibles (is_mock=True, NUNCA métrica inventada). Los futures
                en vuelo terminan acotados por socket_timeout.

        Returns a dict mapping yt_id -> {viewCount, likeCount, commentCount,
        is_mock}. Videos that could not be scraped (private/deleted/
        bot-checked) come back with is_mock=True so callers skip inserting
        them (avoids storing fabricated numbers, unlike the old _mock_stats).
        """
        if not yt_video_ids:
            return {}

        # Preserve order, dedup (a video id repeated would waste requests)
        unique_ids = list(dict.fromkeys(yt_video_ids))
        result: dict[str, dict] = {}
        workers = max(1, min(self.max_concurrency, len(unique_ids)))

        # Ya vencido antes de empezar: no escrapear nada (explícito, no fabricado)
        if deadline is not None and time.time() > deadline:
            logger.warning(
                "[%s] scrape batch: deadline ya superado — %d IDs marcados no disponibles",
                self.slug, len(unique_ids),
            )
            for vid in unique_ids:
                result[vid] = {
                    "viewCount": "0", "likeCount": "0", "commentCount": "0",
                    "is_mock": True,
                }
            result["_deadline_exceeded"] = True
            return result

        def _unavailable(vid: str) -> dict:
            return {
                "viewCount": "0", "likeCount": "0", "commentCount": "0",
                "is_mock": True,
            }

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(self._scrape_one_video, vid): vid
                       for vid in unique_ids}
            done_ids: set[str] = set()
            try:
                # as_completed acota la ESPERA total (no el hilo: los sockets ya
                # tienen su propio timeout; un straggler muere por socket_timeout)
                wait_s = None
                if deadline is not None:
                    wait_s = max(1, int(deadline - time.time()))
                for fut in as_completed(futures, timeout=wait_s):
                    vid = futures[fut]
                    done_ids.add(vid)
                    try:
                        result[vid] = fut.result()
                    except Exception as exc:
                        logger.debug("[%s] scrape error for %s: %s",
                                     self.slug, vid, exc)
                        result[vid] = _unavailable(vid)
            except _FuturesTimeout:
                logger.warning(
                    "[%s] scrape batch: deadline superado (%d/%d completados) "
                    "— resto marcado no disponible",
                    self.slug, len(done_ids), len(unique_ids),
                )
                for fut, vid in futures.items():
                    if vid not in done_ids:
                        fut.cancel()  # cancela los aún no arrancados
                        result[vid] = _unavailable(vid)
                result["_deadline_exceeded"] = True
        return result

    # ── Channel stats ────────────────────────────────────────

    def get_channel_stats(self, channel: dict) -> dict:
        """Scrape channel subscribers from the public channel page.

        Args:
            channel: channel row (dict) with at least yt_channel_id and/or
                     yt_channel_url.

        Returns:
            {subscriberCount, title}. total views and video count are NOT
            available from the public page via yt-dlp (they live on the
            About tab), so they are omitted — the caller should carry forward
            the last known values to avoid false valleys in charts.
        """
        import yt_dlp

        channel_url = channel.get("yt_channel_url")
        if not channel_url:
            yt_channel_id = channel.get("yt_channel_id")
            if yt_channel_id:
                channel_url = f"https://www.youtube.com/channel/{yt_channel_id}"
        if not channel_url:
            return {}

        ydl_opts = {
            **_ydl_base_opts(),
            "skip_download": True,
            "extract_flat": True,
            "playlistend": 1,
            "ignoreerrors": True,
            "logger": _ytdlp_logger,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
        except Exception as exc:
            logger.warning("[%s] channel scrape failed: %s", self.slug, exc)
            return {}

        if not info:
            return {}

        subscribers = _parse_int(info.get("channel_follower_count"))
        return {
            "subscriberCount": str(subscribers),
            "title": info.get("channel") or channel.get("name", ""),
        }
