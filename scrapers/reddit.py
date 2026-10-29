"""Reddit scraper — multi-source with graceful degradation.

Source priority per subreddit (first to yield >0 posts wins):
  1. Arctic Shift mirror    (arctic-shift.photon-reddit.com, no auth)
  2. PullPush mirror        (api.pullpush.io, no auth)
  3. Old Reddit .json       (last resort; often rate-limited from datacenter IPs)

No OAuth account is required or used. A 403 from one source only disables that
source for the rest of the pass; it never aborts the remaining subreddits.
"""

from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable

from scrapers.base import BaseScraper, register_scraper
from scrapers.base import (PermanentBlock, AuthenticationError,
                            RateLimitError, APIUnavailable, ContentNotFound)

if TYPE_CHECKING:
    from database.db import Database


logger = logging.getLogger(__name__)


@register_scraper("reddit")
class RedditScraper(BaseScraper):
    """Scrapes Reddit posts via a priority-ordered chain of sources."""

    MIN_TEXT_LENGTH: int = 200

    # Reddit bloquea (HTTP 403/429) las IPs de datacenter. El scraping funciona
    # por el mirror Arctic Shift (datacenter-friendly) + respaldo por demanda.
    # El proxy de egreso residencial es OPCIONAL y está DESACTIVADO por defecto:
    # actívalo con SCRAPER_USE_EGRESS_PROXY=true si dispones de proxy operativo.
    USE_RESIDENTIAL_PROXY = (
        os.getenv("SCRAPER_USE_EGRESS_PROXY", "false").lower() == "true"
    )

    # Legacy unauthenticated endpoint (bottom of priority chain)
    BASE_URL: str = "https://old.reddit.com"

    # Mirror / fallback endpoints (datacenter-friendly, no auth required)
    PULLPUSH_URL: str = "https://api.pullpush.io/reddit/search/submission/"
    ARCTIC_SHIFT_URL: str = "https://arctic-shift.photon-reddit.com/api/posts/search"

    # Per-source failure tracking. A 403/429 from one mirror must NOT abort
    # the whole channel: other mirrors may still work. Only the failing source
    # is disabled for the remainder of this pass.
    _SOURCE_DEGRADE_AFTER = 3

    def __init__(
        self,
        subreddits: list[str] | None = None,
        sort: str | None = None,
        time_filter: str | None = None,
        limit: int | None = None,
        config: SimpleNamespace | None = None,
        rate_limit: float = 2.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(rate_limit=rate_limit, max_retries=max_retries)

        self._source_failures: dict[str, int] = {}
        self._source_disabled: set[str] = set()

        # ── Subreddits, sort, time, limit ──────────────────────
        # Priority: constructor params > config object > module-level defaults
        from config.defaults import (
            CANAL_NAME as _DEFAULT_CANAL,
            REDDIT_SUBREDDITS as _DEFAULT_SUBS,
            REDDIT_SORT as _DEFAULT_SORT,
            REDDIT_TIME as _DEFAULT_TIME,
            REDDIT_LIMIT as _DEFAULT_LIMIT,
        )

        if config is not None:
            self.subreddits = (
                subreddits
                or getattr(config, "REDDIT_SUBREDDITS", None)
                or getattr(config, "reddit_subreddits", None)
                or _DEFAULT_SUBS
            )
            self.sort = (
                sort
                or getattr(config, "REDDIT_SORT", None)
                or getattr(config, "reddit_sort", None)
                or _DEFAULT_SORT
            )
            self.time_filter = (
                time_filter
                or getattr(config, "REDDIT_TIME", None)
                or getattr(config, "reddit_time", None)
                or _DEFAULT_TIME
            )
            self.limit = (
                limit
                or getattr(config, "REDDIT_LIMIT", None)
                or getattr(config, "reddit_limit", None)
                or _DEFAULT_LIMIT
            )
            self.canal = (
                getattr(config, "CANAL_NAME", None)
                or getattr(config, "canal_name", None)
                or _DEFAULT_CANAL
            )
        else:
            self.subreddits = subreddits or _DEFAULT_SUBS
            self.sort = sort or _DEFAULT_SORT
            self.time_filter = time_filter or _DEFAULT_TIME
            self.limit = limit or _DEFAULT_LIMIT
            self.canal = _DEFAULT_CANAL

        # Build source chain (ordered by priority)
        self._sources: list[tuple[str, Callable[[str], list[dict]]]] = self._build_source_chain()

        self._empty_subreddits: int = 0

        # PullPush-specific circuit breaker: skip after 3 consecutive failures
        self._pullpush_failures: int = 0
        self._pullpush_degraded: bool = False

    # ── Source chain ─────────────────────────────────────────────

    def _build_source_chain(self) -> list[tuple[str, Callable[[str], list[dict]]]]:
        """Return ordered (name, fetcher) pairs. No OAuth account required."""
        return [
            ("arctic_shift", self._fetch_arctic_shift),
            ("pullpush", self._fetch_pullpush),
            ("old_reddit", self._fetch_old_reddit),
        ]

    # ── Main entry point ─────────────────────────────────────────

    def scrape(self) -> list[dict]:
        """Scrape posts from all configured subreddits using available sources.

        Returns:
            List of dicts with keys: source, url, title, text, subreddit, score.
        """
        results: list[dict] = []
        seen_urls: set[str] = set()

        for subreddit in self.subreddits:
            self.logger.info("Scraping r/%s (%s/%s, limit=%d)",
                             subreddit, self.sort, self.time_filter, self.limit)

            posts = self._scrape_subreddit(subreddit)

            for post in posts:
                if post["url"] in seen_urls:
                    continue
                if len(post["text"]) < self.MIN_TEXT_LENGTH:
                    self.logger.debug("Skipping short post: %s", post["url"])
                    continue
                seen_urls.add(post["url"])
                results.append(post)

            self.logger.info("Collected %d valid posts from r/%s (filtered)",
                             len(posts), subreddit)

        self.logger.info("Total scraped: %d posts from %d subreddits",
                         len(results), len(self.subreddits))
        return results

    def save_to_db(self, db: Database) -> int:
        """Scrape and save all posts to the database.

        Args:
            db: Database instance.

        Returns:
            Number of new posts inserted.
        """
        posts = self.scrape()
        inserted = 0
        for post in posts:
            row_id = db.insert_raw_content(
                source=post["source"],
                url=post["url"],
                title=post["title"],
                text=post["text"],
                subreddit=post.get("subreddit"),
                score=post.get("score", 0),
                canal=self.canal,
            )
            if row_id is not None:
                inserted += 1
        self.logger.info("Saved %d/%d posts to database", inserted, len(posts))
        return inserted

    # ── Subreddit-level source cascade ───────────────────────────

    def _scrape_subreddit(self, subreddit: str) -> list[dict]:
        """Try each available source until one yields posts for this subreddit.

        A 403/429 disables only that source; the next source and the next
        subreddit are still attempted. This avoids the old failure mode where
        one blocked mirror aborted all 17 subreddits.
        """
        for source_name, source_fn in self._sources:
            if source_name in self._source_disabled:
                continue
            self.logger.debug("r/%s: trying source '%s'", subreddit, source_name)
            try:
                posts = source_fn(subreddit)
                if posts:
                    self.logger.info("r/%s: %d posts from '%s'",
                                     subreddit, len(posts), source_name)
                    self._empty_subreddits = 0
                    self._source_failures[source_name] = 0
                    return posts
            except PermanentBlock as exc:
                self._source_disabled.add(source_name)
                self.logger.warning(
                    "r/%s: source '%s' blocked (%s) — disabling only this source",
                    subreddit, source_name, exc,
                )
                continue
            except Exception as exc:
                fails = self._source_failures.get(source_name, 0) + 1
                self._source_failures[source_name] = fails
                self.logger.warning("r/%s: source '%s' failed (%d/%d): %s",
                                    subreddit, source_name, fails,
                                    self._SOURCE_DEGRADE_AFTER, exc)
                if fails >= self._SOURCE_DEGRADE_AFTER:
                    self._source_disabled.add(source_name)
                    self.logger.warning(
                        "source '%s' degraded after %d failures — skipping for this pass",
                        source_name, fails,
                    )

        self.logger.warning("r/%s: ALL sources exhausted (0 posts)", subreddit)
        self._empty_subreddits += 1
        return []

    # ── Source: PullPush mirror ──────────────────────────────────

    def _fetch_pullpush(self, subreddit: str) -> list[dict]:
        """Fetch from PullPush Reddit mirror (no auth, datacenter-friendly).

        API: https://api.pullpush.io/reddit/search/submission/
        Returns: {"data": [{post fields ...}], "metadata": {...}}
        Includes circuit breaker: after 3 consecutive failures, skips PullPush.
        """
        if self._pullpush_degraded:
            return []

        params = (
            f"?subreddit={subreddit}"
            f"&sort=desc&sort_type=score"
            f"&size={self.limit}"
            f"&over_18=false"
        )
        url = f"{self.PULLPUSH_URL}{params}"
        try:
            data = self._request(url)
        except (PermanentBlock, AuthenticationError):
            self._pullpush_degraded = True
            self.logger.warning("PullPush permanently blocked — degraded immediately")
            return []
        except (RateLimitError, APIUnavailable):
            self._pullpush_failures += 1
            if self._pullpush_failures >= 3:
                self._pullpush_degraded = True
                self.logger.warning(
                    "🔒 PullPush degraded after %d consecutive failures — skipping for remaining subreddits",
                    self._pullpush_failures,
                )
            return []
        if data is None or not isinstance(data, dict):
            self._pullpush_failures += 1
            if self._pullpush_failures >= 3:
                self._pullpush_degraded = True
                self.logger.warning(
                    "🔒 PullPush degraded after %d consecutive failures — skipping for remaining subreddits",
                    self._pullpush_failures,
                )
            return []

        self._pullpush_failures = 0  # reset on success
        self._pullpush_degraded = False

        posts = data.get("data", [])
        if not isinstance(posts, list):
            return []

        return self._normalize_mirror_posts(posts, subreddit)

    # ── Source: Arctic Shift mirror ──────────────────────────────

    def _fetch_arctic_shift(self, subreddit: str) -> list[dict]:
        """Fetch from Arctic Shift Reddit mirror (no auth, datacenter-friendly).

        API: https://arctic-shift.photon-reddit.com/api/posts/search
        Returns: {"data": [{post fields ...}]}
        """
        params = (
            f"?subreddit={subreddit}"
            f"&limit={self.limit}"
            f"&sort=desc"
        )
        url = f"{self.ARCTIC_SHIFT_URL}{params}"
        data = self._request(url)
        if data is None or not isinstance(data, dict):
            return []

        posts = data.get("data", [])
        if not isinstance(posts, list):
            return []

        return self._normalize_mirror_posts(posts, subreddit)

    # ── Source: Original Old Reddit .json (legacy fallback) ──────

    def _fetch_old_reddit(self, subreddit: str) -> list[dict]:
        """Legacy: Unauthenticated Old Reddit .json endpoint.

        Often blocked (HTTP 403) from datacenter IPs; kept as last resort.
        """
        url = (
            f"{self.BASE_URL}/r/{subreddit}/{self.sort}/.json"
            f"?t={self.time_filter}&limit={self.limit}"
        )
        data = self._request(url)
        if data is None:
            return []
        return self._extract_posts(data, subreddit)

    # ── Normalizers / utils ──────────────────────────────────────

    @staticmethod
    def _extract_posts(data: dict, subreddit: str) -> list[dict]:
        """Parse Reddit JSON listing (data.children[].data) into post dicts.

        Used by: Old Reddit fallback.
        """
        posts: list[dict] = []
        children = data.get("data", {}).get("children", [])
        for child in children:
            kind = child.get("kind", "")
            post_data = child.get("data", {})
            if kind != "t3":
                continue
            if post_data.get("stickied"):
                continue

            permalink = post_data.get("permalink", "")
            text = (post_data.get("selftext") or "").strip()
            title = (post_data.get("title") or "").strip()

            if not title:
                continue

            posts.append({
                "source": "reddit",
                "url": f"{RedditScraper.BASE_URL}{permalink}",
                "title": title,
                "text": text if text else title,
                "subreddit": subreddit,
                "score": post_data.get("score", 0),
            })
        return posts

    @staticmethod
    def _normalize_mirror_posts(posts: list[dict], subreddit: str) -> list[dict]:
        """Convert mirror API post dicts (flat format) to the common schema.

        Mirror sources (PullPush, Arctic Shift) return a flat list under
        "data" key, not data.children[].data. The field names are identical
        to the native Reddit post object.
        """
        out: list[dict] = []
        for p in posts:
            if not isinstance(p, dict):
                continue
            if p.get("stickied"):
                continue

            permalink = (p.get("permalink") or "").strip()
            title = (p.get("title") or "").strip()
            if not title:
                continue

            text = (p.get("selftext") or "").strip()

            out.append({
                "source": "reddit",
                "url": f"https://www.reddit.com{permalink}",
                "title": title,
                "text": text if text else title,
                "subreddit": p.get("subreddit", subreddit),
                "score": p.get("score", 0),
            })
        return out
