"""Wikipedia scraper using the Wikimedia REST API and Action API."""

from __future__ import annotations

import logging
import urllib.parse
from types import SimpleNamespace
from typing import TYPE_CHECKING

from config.canal2_config import (
    CANAL_NAME as _DEFAULT_CANAL,
    WIKIPEDIA_CATEGORIES as _DEFAULT_CATEGORIES,
)
from scrapers.base import BaseScraper, register_scraper

if TYPE_CHECKING:
    from database.db import Database


logger = logging.getLogger(__name__)


@register_scraper("wikipedia")
class WikipediaScraper(BaseScraper):
    """Scrapes Wikipedia articles via the public REST and Action APIs.

    Uses page/random/summary for random articles and action=query with
    list=categorymembers for category-based discovery. No API key required.
    """

    MIN_TEXT_LENGTH: int = 500
    REST_BASE: str = "https://en.wikipedia.org/api/rest_v1"
    ACTION_BASE: str = "https://en.wikipedia.org/w/api.php"

    def __init__(
        self,
        categories: list[str] | None = None,
        random_count: int = 5,
        category_limit: int = 2,
        config: SimpleNamespace | None = None,
        rate_limit: float = 3.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(rate_limit=rate_limit, max_retries=max_retries)
        if config is not None:
            self.categories = categories or getattr(config, "WIKIPEDIA_CATEGORIES", None) or getattr(config, "wikipedia_categories", None) or _DEFAULT_CATEGORIES
            self.canal = getattr(config, "CANAL_NAME", None) or getattr(config, "canal_name", None) or _DEFAULT_CANAL
        else:
            self.categories = categories or _DEFAULT_CATEGORIES
            self.canal = _DEFAULT_CANAL
        self.random_count = random_count
        self.category_limit = category_limit

    def scrape(self) -> list[dict]:
        """Scrape Wikipedia articles from random and category sources.

        Returns:
            List of dicts with keys: source, url, title, text, subreddit, score.
        """
        results: list[dict] = []
        seen_urls: set[str] = set()

        random_pages = self._fetch_random_pages()
        for page in random_pages:
            if page["url"] not in seen_urls:
                seen_urls.add(page["url"])
                results.append(page)

        category_pages = self._fetch_category_pages()
        for page in category_pages:
            if page["url"] not in seen_urls:
                seen_urls.add(page["url"])
                results.append(page)

        self.logger.info("Total scraped: %d articles", len(results))
        return results

    def save_to_db(self, db: Database) -> int:
        """Scrape and save all articles to the database.

        Args:
            db: Database instance.

        Returns:
            Number of new articles inserted.
        """
        articles = self.scrape()
        inserted = 0
        for article in articles:
            row_id = db.insert_raw_content(
                source=article["source"],
                url=article["url"],
                title=article["title"],
                text=article["text"],
                subreddit=None,
                score=0,
                canal=self.canal,
            )
            if row_id is not None:
                inserted += 1
        self.logger.info("Saved %d/%d articles to database", inserted, len(articles))
        return inserted

    def _fetch_random_pages(self) -> list[dict]:
        """Fetch random Wikipedia articles via the REST summary API.

        Returns:
            List of validated article dicts.
        """
        articles: list[dict] = []
        for i in range(self.random_count):
            url = f"{self.REST_BASE}/page/random/summary"
            data = self._request(url)
            if data is None:
                continue

            article = self._build_article(data)
            if article:
                articles.append(article)
                self.logger.debug("Random article %d/%d: %s",
                                  i + 1, self.random_count, article["title"])
        return articles

    def _fetch_category_pages(self) -> list[dict]:
        """Fetch articles from configured Wikipedia categories (max 4 categories).

        Returns:
            List of validated article dicts.
        """
        articles: list[dict] = []
        # Only try first 4 categories to avoid rate limiting
        for category in self.categories[:4]:
            self.logger.info("Fetching category: %s", category)
            titles = self._get_category_members(category)
            for title in titles[:self.category_limit]:
                article = self._fetch_page_summary(title)
                if article:
                    articles.append(article)
        return articles

    def _get_category_members(self, category: str) -> list[str]:
        """Get page titles from a Wikipedia category.

        Args:
            category: Category name (without 'Category:' prefix).

        Returns:
            List of page titles in the category.
        """
        params = urllib.parse.urlencode({
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmlimit": "20",
            "cmtype": "page",
            "format": "json",
        })
        url = f"{self.ACTION_BASE}?{params}"
        data = self._request(url)
        if data is None:
            self.logger.warning("Failed to fetch category: %s", category)
            return []

        members = data.get("query", {}).get("categorymembers", [])
        titles = [m["title"] for m in members if m.get("ns") == 0]
        self.logger.debug("Found %d pages in Category:%s", len(titles), category)
        return titles

    def _fetch_page_summary(self, title: str) -> dict | None:
        """Fetch a single Wikipedia page summary by title.

        Args:
            title: Exact page title.

        Returns:
            Article dict or None if the fetch fails or content is too short.
        """
        encoded_title = urllib.parse.quote(title.replace(" ", "_"))
        url = f"{self.REST_BASE}/page/summary/{encoded_title}"
        data = self._request(url)
        if data is None:
            return None

        article = self._build_article(data)
        if article:
            self.logger.debug("Category article: %s", article["title"])
        return article

    def _build_article(self, data: dict) -> dict | None:
        """Build an article dict from a REST summary API response.

        Args:
            data: Parsed JSON from the page summary endpoint.

        Returns:
            Article dict if valid, None if content is too short or missing.
        """
        title = (data.get("title") or "").strip()
        extract = (data.get("extract") or "").strip()

        if not title or not extract:
            return None
        if len(extract) < self.MIN_TEXT_LENGTH:
            self.logger.debug("Skipping short article: %s (%d chars)",
                              title, len(extract))
            return None

        page_url = (
            data.get("content_urls", {})
            .get("desktop", {})
            .get("page", f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}")
        )

        return {
            "source": "wikipedia",
            "url": page_url,
            "title": title,
            "text": extract,
            "subreddit": None,
            "score": 0,
        }
