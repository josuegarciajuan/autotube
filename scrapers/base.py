"""Base scraper abstract class with rate limiting and retry logic."""

import json
import time
import random
import logging
import urllib.request
import urllib.error
from abc import ABC, abstractmethod


class BaseScraper(ABC):
    """Abstract base for content scrapers.

    Provides shared HTTP request logic with exponential backoff retries
    and configurable rate limiting. Subclasses must implement scrape().
    """

    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    
    # Additional headers to appear more like a browser
    DEFAULT_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "DNT": "1",
    }

    def __init__(self, rate_limit: float = 2.0, max_retries: int = 3) -> None:
        self.rate_limit = rate_limit
        self.max_retries = max_retries
        self._last_request_time: float = 0.0
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def scrape(self) -> list[dict]:
        """Scrape content from the source.

        Returns:
            List of dicts with keys: source, url, title, text, subreddit, score.
        """
        ...

    def _request(self, url: str, headers: dict | None = None) -> dict | list | None:
        """Make an HTTP GET request with retry and rate limiting.

        Args:
            url: The URL to request.
            headers: Optional extra headers (merged with User-Agent).

        Returns:
            Parsed JSON response, or None if all retries fail.
        """
        self._rate_limit()

        default_headers = {"User-Agent": self.USER_AGENT}
        default_headers.update(self.DEFAULT_HEADERS)
        if headers:
            default_headers.update(headers)

        req = urllib.request.Request(url, headers=default_headers)

        for attempt in range(1, self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8")
                    self._last_request_time = time.time()
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                self.logger.warning(
                    "HTTP %d for %s (attempt %d/%d)",
                    e.code, url, attempt, self.max_retries,
                )
                if e.code == 403:
                    self.logger.debug(
                        "403 forbidden for %s — likely IP/datacenter block; "
                        "caller should try alternate source",
                        url,
                    )
                    return None
                if e.code == 429 or e.code >= 500:
                    sleep_time = (2 ** attempt) + random.uniform(0, 1)
                    self.logger.debug("Backing off %.1fs before retry", sleep_time)
                    time.sleep(sleep_time)
                else:
                    return None
            except (urllib.error.URLError, OSError) as e:
                self.logger.warning(
                    "Request failed for %s: %s (attempt %d/%d)",
                    url, e, attempt, self.max_retries,
                )
                if attempt < self.max_retries:
                    sleep_time = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(sleep_time)

        self.logger.error("All %d retries exhausted for %s", self.max_retries, url)
        return None

    def _rate_limit(self) -> None:
        """Sleep if necessary to respect the configured rate limit."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
