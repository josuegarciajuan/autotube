"""Base scraper abstract class with rate limiting, retry logic, and plugin registry."""

import json
import os
import re
import time
import random
import logging
import tempfile
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from types import SimpleNamespace
from typing import Optional

try:  # POSIX-only; degrade to in-process limiting elsewhere
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

logger = logging.getLogger(__name__)

# ── Cross-process per-host rate limiting ──────────────────────────
# Each channel runs in its own worker process, so a per-instance limiter can
# still let N processes hammer the same host (Wikimedia 429).  A file lock per
# host serialises requests across processes and enforces a minimum gap.
_GLOBAL_RATE_LOCK_DIR = os.path.join(tempfile.gettempdir(), "autotube_scraper_limits")
_SCRAPER_CONTACT = os.getenv("SCRAPER_CONTACT", "autotube-bot@localhost")
# Wikimedia asks bots for a descriptive UA with contact info.
API_USER_AGENT = f"Autotube/1.0 (content-pipeline; contact: {_SCRAPER_CONTACT})"
DEFAULT_HOST_MIN_INTERVAL_SEC = float(os.getenv("SCRAPER_HOST_MIN_INTERVAL_SEC", "3.0"))


def global_host_rate_limit(host: str, min_interval: float) -> None:
    """Block until at least ``min_interval`` seconds passed since the last
    request to ``host`` by ANY process.  Fail-open on any error."""
    if fcntl is None or not host:
        return
    interval = max(float(min_interval or 0.0), DEFAULT_HOST_MIN_INTERVAL_SEC)
    try:
        os.makedirs(_GLOBAL_RATE_LOCK_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", host)
        path = os.path.join(_GLOBAL_RATE_LOCK_DIR, f"{safe}.lock")
        with open(path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read().strip()
                try:
                    last = float(raw) if raw else 0.0
                except ValueError:
                    last = 0.0
                wait = interval - (time.time() - last)
                if wait > 0:
                    time.sleep(wait)
                fh.seek(0)
                fh.truncate()
                fh.write(str(time.time()))
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:
        pass


def _retry_after_seconds(err) -> Optional[float]:
    """Parse a ``Retry-After`` header (seconds or HTTP date). None if absent."""
    try:
        value = err.headers.get("Retry-After")  # type: ignore[union-attr]
    except Exception:
        return None
    if not value:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(value))
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None

# ── Plugin registry ────────────────────────────────────────────────

SCRAPER_REGISTRY: dict[str, type] = {}


def register_scraper(name: str):
    """Decorator to register a scraper class in the registry."""
    def decorator(cls):
        SCRAPER_REGISTRY[name] = cls
        logger.info("Registered scraper plugin: %s", name)
        return cls
    return decorator


def get_scraper(name: str, **kwargs):
    """Factory: instantiate a registered scraper by name."""
    if name not in SCRAPER_REGISTRY:
        raise ValueError(f"Unknown scraper: {name}. Available: {list(SCRAPER_REGISTRY.keys())}")
    return SCRAPER_REGISTRY[name](**kwargs)


# ── Custom scraper exceptions ─────────────────────────────────────

class ScraperError(Exception):
    """Base exception for scraper failures."""

class RateLimitError(ScraperError):
    """HTTP 429 — temporary rate limit, retry with backoff."""

class APIUnavailable(ScraperError):
    """HTTP 5xx or connection timeout — service may recover."""

class PermanentBlock(ScraperError):
    """HTTP 403 — permanent IP block, do not retry."""

class AuthenticationError(ScraperError):
    """HTTP 401 — invalid credentials, fatal."""

class ContentNotFound(ScraperError):
    """HTTP 404 — URL does not exist anymore."""


# ── Failure counter ───────────────────────────────────────────────

# Shared across ALL scraper instances for health monitoring
_failure_counts: dict[str, dict] = {}  # source_name → {"failures": int, "last_error": str}


def record_scraper_failure(source_name: str, error_type: str):
    """Increment the failure counter for a source. Logs warning at 10+ failures."""
    if source_name not in _failure_counts:
        _failure_counts[source_name] = {"failures": 0, "last_error": "", "degraded": False}

    c = _failure_counts[source_name]
    c["failures"] += 1
    c["last_error"] = error_type

    if c["failures"] >= 10 and not c["degraded"]:
        c["degraded"] = True
        logger.warning(
            "⚠️  SOURCE DEGRADED: %s — %d consecutive failures (last: %s). "
            "Consider investigating.",
            source_name, c["failures"], error_type,
        )


def reset_scraper_counter(source_name: str):
    """Reset counter on successful fetch."""
    if source_name in _failure_counts:
        _failure_counts[source_name] = {"failures": 0, "last_error": "", "degraded": False}


def get_source_health() -> dict:
    """Return health status of all tracked sources."""
    return dict(_failure_counts)


class BaseScraper(ABC):
    """Abstract base for content scrapers.

    Provides shared HTTP request logic with exponential backoff retries
    and configurable rate limiting. Subclasses must implement scrape().
    """

    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    
    # Additional headers to appear more like a browser.  Wikimedia uses
    # `Api-User-Agent` for bot identification; without contact info it applies
    # aggressive throttling (429).
    DEFAULT_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "DNT": "1",
        "Api-User-Agent": API_USER_AGENT,
    }

    def __init__(
        self,
        config: Optional[SimpleNamespace] = None,
        rate_limit: float = 2.0,
        max_retries: int = 3,
    ) -> None:
        self.config = config
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

    def save_to_db(self, db) -> int:
        """Scrape and save all items to the database.

        Args:
            db: Database instance (must have ``insert_raw_content()``).

        Returns:
            Number of new items inserted.
        """
        items = self.scrape()
        inserted = 0
        # Resolve canal from config or instance attribute
        canal = getattr(
            self,
            "canal",
            getattr(self.config, "CANAL_NAME", None)
            or getattr(self.config, "canal_name", "unknown"),
        )
        for item in items:
            row_id = db.insert_raw_content(
                source=item["source"],
                url=item["url"],
                title=item["title"],
                text=item["text"],
                subreddit=item.get("subreddit"),
                score=item.get("score", 0),
                canal=canal,
            )
            if row_id is not None:
                inserted += 1
        self.logger.info("Saved %d/%d items to database", inserted, len(items))
        return inserted

    def _request(self, url: str, headers: dict | None = None) -> dict | list | None:
        """Make an HTTP GET request with retry and rate limiting.

        Args:
            url: The URL to request.
            headers: Optional extra headers (merged with User-Agent).

        Returns:
            Parsed JSON response, or None if all retries fail.
        """
        self._rate_limit()
        try:
            _host = url.split("/")[2]
        except IndexError:
            _host = ""
        global_host_rate_limit(_host, self.rate_limit)

        default_headers = {"User-Agent": self.USER_AGENT}
        default_headers.update(self.DEFAULT_HEADERS)
        if headers:
            default_headers.update(headers)

        req = urllib.request.Request(url, headers=default_headers)

        for attempt in range(1, self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read().decode("utf-8")
                    self._last_request_time = time.time()
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                self.logger.warning(
                    "HTTP %d for %s (attempt %d/%d)",
                    e.code, url, attempt, self.max_retries,
                )
                if e.code == 403:
                    record_scraper_failure(url.split("/")[2], "HTTP 403 — permanently blocked")
                    raise PermanentBlock(f"HTTP 403 for {url}")
                if e.code == 429:
                    record_scraper_failure(url.split("/")[2], "HTTP 429 — rate limited")
                    if attempt < self.max_retries:
                        retry_after = _retry_after_seconds(e)
                        if retry_after is not None:
                            sleep_time = retry_after + random.uniform(0, 2)
                        else:
                            sleep_time = (5.0 * (2 ** (attempt - 1))) + random.uniform(0, 2)
                        self.logger.warning(
                            "HTTP 429 — backing off %.1fs before retry %d/%d",
                            sleep_time, attempt + 1, self.max_retries,
                        )
                        time.sleep(sleep_time)
                        continue
                    else:
                        raise RateLimitError(f"HTTP 429 for {url} after {self.max_retries} retries")
                if e.code >= 500:
                    record_scraper_failure(url.split("/")[2], f"HTTP {e.code}")
                    if attempt < self.max_retries:
                        sleep_time = (2 ** attempt) + random.uniform(0, 1)
                        time.sleep(sleep_time)
                        continue
                    else:
                        raise APIUnavailable(f"HTTP {e.code} for {url} after {self.max_retries} retries")
                if e.code == 401:
                    raise AuthenticationError(f"HTTP 401 for {url}")
                if e.code == 404:
                    raise ContentNotFound(f"HTTP 404 for {url}")
                return None
            except (urllib.error.URLError, OSError) as e:
                self.logger.warning(
                    "Request failed for %s: %s (attempt %d/%d)",
                    url, e, attempt, self.max_retries,
                )
                record_scraper_failure(url.split("/")[2], str(e)[:100])
                if attempt < self.max_retries:
                    sleep_time = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(sleep_time)

        record_scraper_failure(url.split("/")[2], "All retries exhausted")
        self.logger.error("All %d retries exhausted for %s", self.max_retries, url)
        raise APIUnavailable(f"All {self.max_retries} retries exhausted for {url}")

    def _request_html(self, url: str, headers: dict | None = None) -> str | None:
        """Make an HTTP GET request returning raw HTML text.

        Args:
            url: The URL to request.
            headers: Optional extra headers (merged with User-Agent).

        Returns:
            Raw HTML as string, or None if all retries fail.
        """
        self._rate_limit()
        try:
            _host = url.split("/")[2]
        except IndexError:
            _host = ""
        global_host_rate_limit(_host, self.rate_limit)

        default_headers = {"User-Agent": self.USER_AGENT}
        default_headers.update(self.DEFAULT_HEADERS)
        if headers:
            default_headers.update(headers)

        req = urllib.request.Request(url, headers=default_headers)

        for attempt in range(1, self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    self._last_request_time = time.time()
                    return body
            except urllib.error.HTTPError as e:
                self.logger.warning(
                    "HTTP %d for %s (attempt %d/%d)",
                    e.code, url, attempt, self.max_retries,
                )
                if e.code == 403:
                    self.logger.debug("403 forbidden for %s — likely block", url)
                    return None
                if e.code == 429 or e.code >= 500:
                    retry_after = _retry_after_seconds(e) if e.code == 429 else None
                    if retry_after is not None:
                        sleep_time = retry_after + random.uniform(0, 2)
                    else:
                        sleep_time = (5.0 * (2 ** (attempt - 1))) + random.uniform(0, 2)
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
