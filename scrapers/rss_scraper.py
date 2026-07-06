"""Generic RSS feed scraper — parses arbitrary RSS/Atom feeds.

Uses the ``feedparser`` library to fetch and parse RSS/Atom feeds.
Each channel config can supply a list of feed URLs via ``rss_feeds``.

Output format follows the standard scraper contract:
{"source": "rss", "url": str, "title": str, "text": str, "subreddit": None, "score": 0}
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

from bs4 import BeautifulSoup

from config.canal2_config import CANAL_NAME as _DEFAULT_CANAL
from scrapers.base import BaseScraper, register_scraper

if TYPE_CHECKING:
    from database.db import Database


logger = logging.getLogger(__name__)

# Default fallback feeds (used when no channel config is available)
DEFAULT_FEEDS: list[str] = []


@register_scraper("rss")
class RSSScraper(BaseScraper):
    """Scrapes arbitrary RSS/Atom feeds for content."""

    MIN_TEXT_LENGTH: int = 200

    def __init__(
        self,
        feeds: list[str] | None = None,
        config: Optional[SimpleNamespace] = None,
        rate_limit: float = 2.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(config=config, rate_limit=rate_limit, max_retries=max_retries)

        if config is not None:
            self.feeds = (
                feeds
                or getattr(config, "RSS_FEEDS", None)
                or getattr(config, "rss_feeds", None)
                or DEFAULT_FEEDS
            )
            self.canal = (
                getattr(config, "CANAL_NAME", None)
                or getattr(config, "canal_name", None)
                or _DEFAULT_CANAL
            )
        else:
            self.feeds = feeds or DEFAULT_FEEDS
            self.canal = _DEFAULT_CANAL

    # ── Main entry point ─────────────────────────────────────────

    def scrape(self) -> list[dict]:
        """Scrape all configured RSS feeds.

        Returns:
            List of dicts with keys: source, url, title, text, subreddit, score.
        """
        try:
            import feedparser
        except ImportError:
            self.logger.error("feedparser not installed — install with: pip install feedparser")
            return []

        results: list[dict] = []
        seen_urls: set[str] = set()

        if not self.feeds:
            self.logger.warning("No RSS feeds configured")
            return []

        for feed_url in self.feeds:
            self.logger.info("Fetching RSS feed: %s", feed_url)
            try:
                items = self._parse_feed(feedparser, feed_url)
            except Exception as exc:
                self.logger.warning("RSS feed '%s' failed: %s", feed_url, exc)
                items = []

            for item in items:
                if item["url"] in seen_urls:
                    continue
                if len(item["text"]) < self.MIN_TEXT_LENGTH:
                    self.logger.debug("Skipping short RSS item: %s", item["url"])
                    continue
                seen_urls.add(item["url"])
                results.append(item)

        self.logger.info("RSS: total %d items from %d feeds",
                         len(results), len(self.feeds))
        return results

    # ── Feed parsing ─────────────────────────────────────────────

    def _parse_feed(self, feedparser, feed_url: str) -> list[dict]:
        """Parse a single RSS/Atom feed and extract entries.

        Args:
            feedparser: The feedparser module.
            feed_url: URL of the RSS/Atom feed.

        Returns:
            List of normalized item dicts.
        """
        items: list[dict] = []

        feed = feedparser.parse(feed_url)
        if feed.bozo and not feed.entries:
            self.logger.debug("Feed '%s' is malformed/empty (bozo=%s)",
                              feed_url, getattr(feed.bozo_exception, "getMessage", lambda: "?")())
            return []

        entries = feed.entries[:20]  # limit per feed

        for entry in entries:
            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()

            # Collect text from various possible fields
            summary = (getattr(entry, "summary", "") or "").strip()
            desc = (getattr(entry, "description", "") or "").strip()
            content_list = getattr(entry, "content", None)
            content_text = ""
            if content_list:
                for c in content_list:
                    if hasattr(c, "value"):
                        content_text += c.value + " "

            text = summary or desc or content_text or ""

            # Clean HTML tags from text
            if text:
                soup = BeautifulSoup(text, "html.parser")
                text = soup.get_text(" ", strip=True)

            if not title:
                continue

            # Resolve link: some feeds put it in the `link` attr directly,
            # others nest it in `links` list
            if not link:
                links = getattr(entry, "links", [])
                for l in links:
                    href = l.get("href", "") if isinstance(l, dict) else getattr(l, "href", "")
                    if href:
                        link = href
                        break

            items.append({
                "source": "rss",
                "url": link or feed_url,
                "title": title[:300],
                "text": text if text else title,
                "subreddit": None,
                "score": getattr(entry, "score", 0) or 0,
            })

        return items
