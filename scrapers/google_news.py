"""Google News scraper — fetches articles via Google News RSS feeds.

Uses the ``feedparser`` library to consume Google News RSS search results.
Each channel config can supply search queries via ``google_news_queries``.

RSS feed format:
  https://news.google.com/rss/search?q={query}&hl={lang}&gl={country}&ceid={country}:{lang}

Output follows the standard scraper contract:
{"source": "google_news", "url": str, "title": str, "text": str, "subreddit": None, "score": 0}
"""

from __future__ import annotations

import logging
import urllib.parse
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

from bs4 import BeautifulSoup

from config.canal2_config import CANAL_NAME as _DEFAULT_CANAL
from scrapers.base import BaseScraper, register_scraper

if TYPE_CHECKING:
    from database.db import Database


logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q={query}&hl={lang}&gl={country}&ceid={country}:{lang}"
)

# Default queries (used when no channel config is available)
DEFAULT_QUERIES: list[str] = []


@register_scraper("google_news")
class GoogleNewsScraper(BaseScraper):
    """Scrapes Google News RSS for current events and trending stories."""

    MIN_TEXT_LENGTH: int = 200

    def __init__(
        self,
        queries: list[str] | None = None,
        language: str = "es",
        country: str = "ES",
        config: Optional[SimpleNamespace] = None,
        rate_limit: float = 3.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(config=config, rate_limit=rate_limit, max_retries=max_retries)

        if config is not None:
            self.queries = (
                queries
                or getattr(config, "GOOGLE_NEWS_QUERIES", None)
                or getattr(config, "google_news_queries", None)
                or DEFAULT_QUERIES
            )
            self.language = (
                getattr(config, "GOOGLE_NEWS_LANGUAGE", None)
                or getattr(config, "google_news_language", None)
                or language
            )
            self.country = (
                getattr(config, "GOOGLE_NEWS_COUNTRY", None)
                or getattr(config, "google_news_country", None)
                or country
            )
            self.canal = (
                getattr(config, "CANAL_NAME", None)
                or getattr(config, "canal_name", None)
                or _DEFAULT_CANAL
            )
        else:
            self.queries = queries or DEFAULT_QUERIES
            self.language = language
            self.country = country
            self.canal = _DEFAULT_CANAL

    # ── Main entry point ─────────────────────────────────────────

    def scrape(self) -> list[dict]:
        """Scrape Google News for all configured queries.

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

        if not self.queries:
            self.logger.warning("No Google News queries configured")
            return []

        for query in self.queries:
            self.logger.info("Google News query: '%s' (lang=%s, country=%s)",
                             query, self.language, self.country)
            try:
                items = self._fetch_query(feedparser, query)
            except Exception as exc:
                self.logger.warning("Google News query '%s' failed: %s", query, exc)
                items = []

            for item in items:
                if item["url"] in seen_urls:
                    continue
                if len(item["text"]) < self.MIN_TEXT_LENGTH:
                    self.logger.debug("Skipping short Google News item: %s", item["url"])
                    continue
                seen_urls.add(item["url"])
                results.append(item)

        self.logger.info("Google News: total %d items from %d queries",
                         len(results), len(self.queries))
        return results

    # ── Query execution ──────────────────────────────────────────

    def _fetch_query(self, feedparser, query: str) -> list[dict]:
        """Execute a single Google News search and return parsed entries.

        Args:
            feedparser: The feedparser module.
            query: Search query string.

        Returns:
            List of normalized item dicts.
        """
        encoded_query = urllib.parse.quote(query)
        url = GOOGLE_NEWS_RSS.format(
            query=encoded_query,
            lang=self.language,
            country=self.country,
        )

        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            self.logger.debug("Google News RSS empty for query '%s'", query)
            return []

        items: list[dict] = []
        for entry in feed.entries[:15]:
            title = (getattr(entry, "title", "") or "").strip()

            # Google News RSS title format: "Title - Source Name"
            # Strip trailing source attribution for cleaner titles
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                clean_title = parts[0].strip()
            else:
                clean_title = title

            link = (getattr(entry, "link", "") or "").strip()
            summary = (getattr(entry, "summary", "") or "").strip()
            desc = (getattr(entry, "description", "") or "").strip()

            text = summary or desc or ""
            if text:
                # Clean HTML tags from text
                soup = BeautifulSoup(text, "html.parser")
                text = soup.get_text(" ", strip=True)

            if not clean_title:
                continue

            items.append({
                "source": "google_news",
                "url": link,
                "title": clean_title[:300],
                "text": text if text else clean_title,
                "subreddit": None,
                "score": 0,
            })

        return items
