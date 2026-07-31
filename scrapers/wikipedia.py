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

    def scrape(self, mode: str = "summary", config=None, limit: int = None,
               deep_category_limit: int = 15) -> list[dict]:
        """Scrape Wikipedia articles from random and category sources.

        Args:
            mode: "summary" (default) uses REST API summaries (~500 chars).
                  "deep" fetches full HTML pages with rich content (~2000-8000 chars).
            config: Optional channel config for overriding categories.
            limit: Optional override for category_limit (articles per category).
            deep_category_limit: Max categories to fetch in deep mode.

        Returns:
            List of dicts with keys: source, url, title, text, subreddit, score.
        """
        # Allow config to override categories
        if config is not None:
            cats = getattr(config, "WIKIPEDIA_CATEGORIES", None)
            if cats:
                self.categories = cats

        if mode == "deep":
            return self._scrape_deep(
                config=config, 
                category_limit=limit or 15,
                deep_category_limit=deep_category_limit,
            )

        # ── Normal (summary) mode ──
        if limit is not None:
            self.category_limit = limit

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

    def _scrape_deep(self, config=None, category_limit: int = 15,
                     deep_category_limit: int = 15) -> list[dict]:
        """Deep scrape: fetch full HTML pages from Wikipedia categories.

        Yields rich, long-form content (~2000-8000 chars per article) suitable
        for marathon video scripts. Fetches many more articles than normal mode.

        Returns:
            List of article dicts with full text.
        """
        results: list[dict] = []
        seen_urls: set[str] = set()

        # Fetch from many categories (deep mode)
        cats_to_fetch = self.categories[:deep_category_limit]
        if not cats_to_fetch:
            cats_to_fetch = self.categories[:4]  # fallback

        self.logger.info("Deep scrape: %d categories, %d articles each",
                         len(cats_to_fetch), category_limit)

        for category in cats_to_fetch:
            try:
                titles = self._get_category_members(category)
                self.logger.debug("Category '%s': %d titles", category, len(titles))
                
                fetched = 0
                for title in titles[:category_limit]:
                    article = self._fetch_page_full(title)
                    if article and article["url"] not in seen_urls:
                        seen_urls.add(article["url"])
                        results.append(article)
                        fetched += 1
                
                self.logger.debug("Category '%s': fetched %d deep articles", category, fetched)
            except Exception as exc:
                self.logger.warning("Deep scrape: category '%s' failed: %s", category, exc)

        self.logger.info("Deep scrape: %d total articles (%d chars avg)",
                         len(results),
                         sum(len(a["text"]) for a in results) // max(1, len(results)))
        return results

    def _fetch_page_full(self, title: str) -> dict | None:
        """Fetch a full Wikipedia page via the mobile-html API and extract text.

        Uses the page/mobile-html endpoint which returns cleaner HTML than
        the desktop version (no infoboxes, navboxes, or sidebar clutter).

        Args:
            title: Exact Wikipedia page title.

        Returns:
            Article dict with rich text, or None if fetch fails.
        """
        import re as _re

        encoded_title = urllib.parse.quote(title.replace(" ", "_"))
        url = f"{self.REST_BASE}/page/mobile-html/{encoded_title}"

        resp = self._request_raw(url)
        if resp is None:
            return None

        html_content = resp
        if not html_content or len(html_content) < 500:
            self.logger.debug("Deep scrape: short/no content for '%s'", title)
            return None

        # Simple HTML to text extraction
        try:
            import re
            # Remove script and style tags
            text = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            # Remove HTML tags
            text = re.sub(r'<[^>]+>', ' ', text)
            # Collapse whitespace
            text = re.sub(r'\s+', ' ', text).strip()
            # Decode HTML entities
            import html as _html
            text = _html.unescape(text)
        except Exception:
            # Fallback: just strip tags
            text = _re.sub(r'<[^>]+>', ' ', html_content)
            text = _re.sub(r'\s+', ' ', text).strip()

        if len(text) < 200:
            self.logger.debug("Deep scrape: text too short for '%s' (%d chars)", title, len(text))
            return None

        page_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"

        self.logger.debug("Deep scrape: '%s' → %d chars", title, len(text))

        return {
            "source": "wikipedia_deep",
            "url": page_url,
            "title": title,
            "text": text[:8000],  # Cap at 8000 chars to avoid huge prompts
            "subreddit": None,
            "score": 1,
        }

    def _request_raw(self, url: str) -> str | None:
        """Make an HTTP request and return raw response text (for HTML).

        Returns response body as string, or None on failure.
        """
        import time as _time
        for attempt in range(self.max_retries + 1):
            try:
                import urllib.request as _ur
                req = _ur.Request(url, headers={"User-Agent": self.USER_AGENT})
                with _ur.urlopen(req, timeout=30) as resp:
                    content = resp.read()
                    # Try to decode
                    charset = resp.headers.get_content_charset() or 'utf-8'
                    return content.decode(charset, errors='replace')
            except Exception as exc:
                self.logger.warning(
                    "Request attempt %d/%d for %s failed: %s",
                    attempt + 1, self.max_retries + 1, url, exc,
                )
                if attempt < self.max_retries:
                    _time.sleep(self.rate_limit)
        return None

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
