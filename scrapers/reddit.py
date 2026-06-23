"""Reddit scraper — multi-source with graceful degradation.

Source priority per subreddit (first to yield >0 posts wins):
  1. Reddit OAuth API (if REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET configured)
  2. PullPush mirror        (api.pullpush.io, no auth, datacenter-friendly)
  3. Arctic Shift mirror    (arctic-shift.photon-reddit.com, no auth)
  4. Old Reddit .json       (unauthenticated, often blocked from datacenter IPs)
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable

from config.settings import (
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
    REDDIT_USER_AGENT,
)
from scrapers.base import BaseScraper

if TYPE_CHECKING:
    from database.db import Database


logger = logging.getLogger(__name__)


class RedditScraper(BaseScraper):
    """Scrapes Reddit posts via a priority-ordered chain of sources."""

    MIN_TEXT_LENGTH: int = 200

    # Legacy unauthenticated endpoint (bottom of priority chain)
    BASE_URL: str = "https://old.reddit.com"

    # Mirror / fallback endpoints (datacenter-friendly, no auth required)
    PULLPUSH_URL: str = "https://api.pullpush.io/reddit/search/submission/"
    ARCTIC_SHIFT_URL: str = "https://arctic-shift.photon-reddit.com/api/posts/search"

    # Official OAuth endpoint
    OAUTH_BASE: str = "https://oauth.reddit.com"
    OAUTH_TOKEN_URL: str = "https://www.reddit.com/api/v1/access_token"

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

        # ── Source chain construction ──────────────────────────
        # Resolve OAuth credentials (from top-level config module)
        self.reddit_client_id: str = REDDIT_CLIENT_ID or ""
        self.reddit_client_secret: str = REDDIT_CLIENT_SECRET or ""
        self.reddit_agent: str = REDDIT_USER_AGENT or self.USER_AGENT

        self._reddit_token: str | None = None
        self._reddit_token_expiry: float = 0.0

        # ── Subreddits, sort, time, limit ──────────────────────
        # Priority: constructor params > config object > module-level defaults
        from config.canal1_config import (
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

    # ── Source chain ─────────────────────────────────────────────

    def _build_source_chain(self) -> list[tuple[str, Callable[[str], list[dict]]]]:
        """Return ordered (name, fetcher) pairs based on available credentials."""
        chain: list[tuple[str, Callable[[str], list[dict]]]] = []

        if self.reddit_client_id and self.reddit_client_secret:
            chain.append(("oauth", self._fetch_oauth))

        chain.append(("pullpush", self._fetch_pullpush))
        chain.append(("arctic_shift", self._fetch_arctic_shift))
        chain.append(("old_reddit", self._fetch_old_reddit))
        return chain

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
        """Try each available source until one yields posts for this subreddit."""
        for source_name, source_fn in self._sources:
            self.logger.debug("r/%s: trying source '%s'", subreddit, source_name)
            try:
                posts = source_fn(subreddit)
                if posts:
                    self.logger.info("r/%s: %d posts from '%s'",
                                     subreddit, len(posts), source_name)
                    return posts
            except Exception as exc:
                self.logger.warning("r/%s: source '%s' failed: %s",
                                    subreddit, source_name, exc)

        self.logger.warning("r/%s: ALL sources exhausted (0 posts)", subreddit)
        return []

    # ── Source: Official Reddit OAuth API ────────────────────────

    def _get_oauth_token(self) -> str | None:
        """Obtain or reuse a Reddit OAuth app-only access token.

        Uses 'client_credentials' grant (application-only, read-only access).
        Token is cached and reused until ~1 minute before expiry.
        """
        if not self.reddit_client_id or not self.reddit_client_secret:
            return None

        now = time.time()
        # Reuse token if it's still valid (with 60s safety margin)
        if self._reddit_token and now < (self._reddit_token_expiry - 60):
            return self._reddit_token

        self.logger.info("Requesting new Reddit OAuth token...")
        try:
            import requests

            resp = requests.post(
                self.OAUTH_TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.reddit_client_id, self.reddit_client_secret),
                headers={"User-Agent": self.reddit_agent},
                timeout=15,
            )
            resp.raise_for_status()
            token_data = resp.json()

            self._reddit_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", 3600)
            self._reddit_token_expiry = now + expires_in

            self.logger.info("OAuth token obtained (expires in %ds)", expires_in)
            return self._reddit_token
        except Exception as exc:
            self.logger.warning("OAuth token request failed: %s", exc)
            return None

    def _fetch_oauth(self, subreddit: str) -> list[dict]:
        """Fetch posts via the official Reddit OAuth API."""
        token = self._get_oauth_token()
        if not token:
            return []

        url = (
            f"{self.OAUTH_BASE}/r/{subreddit}/{self.sort}/.json"
            f"?t={self.time_filter}&limit={self.limit}"
        )
        headers = {
            "Authorization": f"bearer {token}",
            "User-Agent": self.reddit_agent,
        }
        data = self._request(url, headers=headers)
        if data is None:
            return []
        return self._extract_posts(data, subreddit)

    # ── Source: PullPush mirror ──────────────────────────────────

    def _fetch_pullpush(self, subreddit: str) -> list[dict]:
        """Fetch from PullPush Reddit mirror (no auth, datacenter-friendly).

        API: https://api.pullpush.io/reddit/search/submission/
        Returns: {"data": [{post fields ...}], "metadata": {...}}
        """
        params = (
            f"?subreddit={subreddit}"
            f"&sort=desc&sort_type=score"
            f"&size={self.limit}"
            f"&over_18=false"
        )
        url = f"{self.PULLPUSH_URL}{params}"
        data = self._request(url)
        if data is None or not isinstance(data, dict):
            return []

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

        Used by: OAuth source and Old Reddit fallback.
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
