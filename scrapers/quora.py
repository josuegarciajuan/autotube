"""Quora scraper — personal stories and answers from Quora.

Strategy (multi-layer fallback):
  1. Quora topic RSS feeds (some topics expose RSS)
  2. Google search proxy: site:quora.com {query}
  3. Direct topic page scraping with requests + BeautifulSoup

Quora aggressively blocks datacenter IPs, so we always degrade
gracefully and return whatever partial results we can obtain.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

from bs4 import BeautifulSoup

from config.defaults import CANAL_NAME as _DEFAULT_CANAL
from scrapers.base import BaseScraper, register_scraper

if TYPE_CHECKING:
    from database.db import Database

logger = logging.getLogger(__name__)

QUORA_TOPIC_RSS = "https://www.quora.com/topic/{topic}/rss"
QUORA_SEARCH = "https://www.quora.com/search?q={query}&type=answer"
GOOGLE_SITE_SEARCH = "https://www.google.com/search?q=site:quora.com+{query}"

DEFAULT_TOPICS: list[str] = [
    "Personal-Experiences",
    "Life-Stories",
    "Human-Psychology",
    "Mysteries",
    "Unexplained-Phenomena",
]


@register_scraper("quora")
class QuoraScraper(BaseScraper):
    """Scrapes Quora for personal stories and in-depth answers."""

    MIN_TEXT_LENGTH: int = 200

    def __init__(
        self,
        topics: list[str] | None = None,
        config: Optional[SimpleNamespace] = None,
        rate_limit: float = 5.0,
        max_retries: int = 2,
    ) -> None:
        super().__init__(config=config, rate_limit=rate_limit, max_retries=max_retries)

        if config is not None:
            self.topics = (
                topics
                or getattr(config, "QUORA_TOPICS", None)
                or getattr(config, "quora_topics", None)
                or DEFAULT_TOPICS
            )
            self.canal = (
                getattr(config, "CANAL_NAME", None)
                or getattr(config, "canal_name", None)
                or _DEFAULT_CANAL
            )
        else:
            self.topics = topics or DEFAULT_TOPICS
            self.canal = _DEFAULT_CANAL

    # ── Main entry point ─────────────────────────────────────────

    def scrape(self) -> list[dict]:
        """Scrape answers/stories from Quora topics.

        Returns:
            List of dicts with keys: source, url, title, text, subreddit, score.
        """
        results: list[dict] = []
        seen_urls: set[str] = set()

        for topic in self.topics[:5]:  # limit topics to avoid rate-limiting
            self.logger.info("Scraping Quora topic: %s", topic)
            try:
                items = self._scrape_topic(topic)
            except Exception as exc:
                self.logger.warning("Quora topic '%s' failed: %s", topic, exc)
                items = []

            for item in items:
                if item["url"] in seen_urls:
                    continue
                if len(item["text"]) < self.MIN_TEXT_LENGTH:
                    continue
                seen_urls.add(item["url"])
                results.append(item)

            # Be polite — long delay between topics
            time.sleep(3)

        self.logger.info("Quora: total %d items from %d topics",
                         len(results), len(self.topics))
        return results

    # ── Topic scraping ───────────────────────────────────────────

    def _scrape_topic(self, topic: str) -> list[dict]:
        """Try multiple approaches for a single topic."""
        # 1. Try RSS first (most reliable)
        items = self._scrape_rss(topic)
        if items:
            return items

        # 2. Try Google search proxy
        items = self._scrape_google_proxy(topic)
        if items:
            return items

        # 3. Try direct search page scraping
        return self._scrape_search_page(topic)

    def _scrape_rss(self, topic: str) -> list[dict]:
        """Fetch Quora topic RSS feed.

        Only some topics expose RSS. Returns empty list on failure.
        """
        try:
            import feedparser
        except ImportError:
            self.logger.debug("feedparser not available — skipping RSS")
            return []

        encoded_topic = urllib.parse.quote(topic)
        url = QUORA_TOPIC_RSS.format(topic=encoded_topic)

        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            self.logger.debug("RSS parse failed for topic '%s': %s", topic, exc)
            return []

        if feed.bozo and not feed.entries:
            self.logger.debug("RSS feed invalid/empty for topic '%s'", topic)
            return []

        items: list[dict] = []
        for entry in feed.entries[:10]:
            title = (getattr(entry, "title", "") or "").strip()
            text = (getattr(entry, "summary", "") or getattr(entry, "description", "") or "").strip()
            url_val = (getattr(entry, "link", "") or "").strip()

            if not title or not url_val:
                continue

            # Clean HTML from summary
            from html import unescape
            text = BeautifulSoup(text, "html.parser").get_text(" ")
            text = unescape(text).strip()

            items.append({
                "source": "quora",
                "url": url_val,
                "title": title,
                "text": text if text else title,
                "subreddit": topic,
                "score": 0,
            })

        return items

    def _scrape_google_proxy(self, topic: str) -> list[dict]:
        """Use Google search as a proxy to find Quora pages.

        This is fragile and may be blocked. Returns empty on failure.
        """
        query = urllib.parse.quote(f"site:quora.com {topic}")
        url = GOOGLE_SITE_SEARCH.format(query=query)

        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        html = self._request_html(url, headers=headers)
        if not html:
            return []

        items: list[dict] = []
        soup = BeautifulSoup(html, "lxml")

        for result in soup.select("a[href*='quora.com']")[:10]:
            href = result.get("href", "")
            title_el = result.select_one("h3")
            snippet_el = result.select_one("div[data-sncf], span.st")

            if not href:
                continue
            if not href.startswith("http"):
                continue

            title = title_el.get_text(strip=True) if title_el else ""
            text = snippet_el.get_text(strip=True) if snippet_el else ""

            if not title:
                continue

            items.append({
                "source": "quora",
                "url": href,
                "title": title,
                "text": text if text else title,
                "subreddit": topic,
                "score": 0,
            })

        return items

    def _scrape_search_page(self, topic: str) -> list[dict]:
        """Directly scrape Quora's own search page."""
        query = urllib.parse.quote(topic)
        url = QUORA_SEARCH.format(query=query)

        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        html = self._request_html(url, headers=headers)
        if not html:
            return []

        items: list[dict] = []
        soup = BeautifulSoup(html, "lxml")

        # Quora uses various selectors — try common ones
        for selector in ("div.q-box.qu-borderBottom", "div.puppeteer_test_answer_content",
                         "div.q-text", "span.q-box.qu-userSelect--text"):
            for el in soup.select(selector):
                link = el.select_one("a[href]")
                text = el.get_text(" ", strip=True)
                title = ""
                url_val = ""

                if link:
                    href = link.get("href", "")
                    if href:
                        url_val = "https://www.quora.com" + href if not href.startswith("http") else href
                        title = link.get_text(strip=True)

                if not title or len(text) < self.MIN_TEXT_LENGTH:
                    continue

                items.append({
                    "source": "quora",
                    "url": url_val or url,
                    "title": title[:200],
                    "text": text[:5000],
                    "subreddit": topic,
                    "score": 0,
                })
            if items:
                break

        return items
