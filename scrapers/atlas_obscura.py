"""Atlas Obscura scraper — unique places, mysteries, and stories.

Scrapes article listings from atlasobscura.com for content about
unusual places, hidden history, and strange phenomena. Perfect
source for mystery/wonder niches.

Uses:
  - Sitemap / articles feed (preferred — machine-friendly)
  - Direct page scraping with requests + BeautifulSoup (fallback)
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

from bs4 import BeautifulSoup

from config.canal2_config import CANAL_NAME as _DEFAULT_CANAL
from scrapers.base import BaseScraper, register_scraper

if TYPE_CHECKING:
    from database.db import Database


logger = logging.getLogger(__name__)

ATLAS_OBSCURA_BASE = "https://www.atlasobscura.com"
ATLAS_OBSCURA_ARTICLES = f"{ATLAS_OBSCURA_BASE}/articles"
ATLAS_OBSCURA_SITEMAP = f"{ATLAS_OBSCURA_BASE}/sitemap.xml"

DEFAULT_CATEGORIES: list[str] = [
    "wonders",
    "nature",
    "history",
    "mysteries",
]


@register_scraper("atlas_obscura")
class AtlasObscuraScraper(BaseScraper):
    """Scrapes Atlas Obscura for unique and mysterious place-based stories."""

    MIN_TEXT_LENGTH: int = 200

    def __init__(
        self,
        categories: list[str] | None = None,
        config: Optional[SimpleNamespace] = None,
        rate_limit: float = 3.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(config=config, rate_limit=rate_limit, max_retries=max_retries)

        if config is not None:
            self.categories = (
                categories
                or getattr(config, "ATLAS_OBSCURA_CATEGORIES", None)
                or getattr(config, "atlas_obscura_categories", None)
                or DEFAULT_CATEGORIES
            )
            self.canal = (
                getattr(config, "CANAL_NAME", None)
                or getattr(config, "canal_name", None)
                or _DEFAULT_CANAL
            )
        else:
            self.categories = categories or DEFAULT_CATEGORIES
            self.canal = _DEFAULT_CANAL

    # ── Main entry point ─────────────────────────────────────────

    def scrape(self) -> list[dict]:
        """Scrape articles from Atlas Obscura.

        Returns:
            List of dicts with keys: source, url, title, text, subreddit, score.
        """
        results: list[dict] = []
        seen_urls: set[str] = set()

        # 1. Try the main articles listing page
        self.logger.info("Scraping Atlas Obscura articles page...")
        try:
            items = self._scrape_articles_page()
            for item in items:
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    results.append(item)
        except Exception as exc:
            self.logger.warning("Atlas Obscura articles page failed: %s", exc)

        # 2. If categories configured, try category pages
        for cat in self.categories[:4]:
            self.logger.info("Scraping Atlas Obscura category: %s", cat)
            try:
                items = self._scrape_category(cat)
                for item in items:
                    if item["url"] not in seen_urls and len(item["text"]) >= self.MIN_TEXT_LENGTH:
                        seen_urls.add(item["url"])
                        results.append(item)
            except Exception as exc:
                self.logger.warning("Atlas Obscura category '%s' failed: %s", cat, exc)
            time.sleep(2)

        self.logger.info("Atlas Obscura: total %d items", len(results))
        return results

    # ── Page scraping helpers ─────────────────────────────────────

    def _scrape_articles_page(self) -> list[dict]:
        """Scrape the main /articles listing page."""
        items: list[dict] = []

        html = self._request_html(ATLAS_OBSCURA_ARTICLES)
        if not html:
            return items

        soup = BeautifulSoup(html, "lxml")

        # Atlas Obscura article cards: various selectors
        article_selectors = [
            "a[href*='/articles/']",
            "div[class*='Card'] a[href*='/articles/']",
            "article a[href*='/articles/']",
            "div[class*='article-card'] a",
            "a[class*='ArticleCard']",
            "a[class*='Card__link']",
        ]

        seen: set[str] = set()
        for selector in article_selectors:
            for link in soup.select(selector):
                href = link.get("href", "")
                if not href or "/articles/" not in href:
                    continue

                full_url = href if href.startswith("http") else ATLAS_OBSCURA_BASE + href
                if full_url in seen:
                    continue
                seen.add(full_url)

                # Extract title and description from parent container
                title = link.get_text(strip=True)
                parent = link.find_parent(["article", "div", "li"])
                desc = ""
                if parent:
                    desc_el = parent.select_one("div[class*='description'], p, div[class*='dek'], div[class*='subtitle']")
                    if desc_el:
                        desc = desc_el.get_text(" ", strip=True)

                if not title:
                    continue

                items.append({
                    "source": "atlas_obscura",
                    "url": full_url,
                    "title": title[:300],
                    "text": desc if desc else title,
                    "subreddit": None,
                    "score": 0,
                })

            if items:
                break

        return items

    def _scrape_category(self, category: str) -> list[dict]:
        """Scrape a specific category page on Atlas Obscura."""
        items: list[dict] = []
        encoded_cat = urllib.parse.quote(category.lower().replace(" ", "-"))
        url = f"{ATLAS_OBSCURA_BASE}/categories/{encoded_cat}"

        html = self._request_html(url)
        if not html:
            return items

        soup = BeautifulSoup(html, "lxml")

        for link in soup.select("a[href*='/articles/']"):
            href = link.get("href", "")
            if not href or "/articles/" not in href:
                continue

            full_url = href if href.startswith("http") else ATLAS_OBSCURA_BASE + href
            title = link.get_text(strip=True)

            # Try to find a description nearby
            parent = link.find_parent(["div", "article", "li", "section"])
            desc = ""
            if parent:
                desc_el = parent.select_one("p, div[class*='description'], div[class*='summary']")
                if desc_el:
                    desc = desc_el.get_text(" ", strip=True)

            if not title:
                continue

            items.append({
                "source": "atlas_obscura",
                "url": full_url,
                "title": title[:300],
                "text": desc if desc else title,
                "subreddit": category,
                "score": 0,
            })

            if len(items) >= 10:
                break

        return items
