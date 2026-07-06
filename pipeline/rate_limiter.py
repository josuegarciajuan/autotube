"""Token-bucket rate limiter for API providers (Unsplash, Pexels, etc.).

Prevents hitting provider rate limits by tracking requests per sliding
one-hour window and sleeping proactively when needed. Thread-safe.

v2 (Jul 2026): singleton instances shared globally per provider name
so that MediaFetcher and ImageFetcher share the same counter.  Also
adds ``try_acquire()`` — a non-blocking alternative that returns False
when the window is exhausted so callers can skip to a fallback provider
instead of sleeping.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Default rate limits (requests per hour) — conservative for free tiers
DEFAULT_LIMITS: dict[str, int] = {
    "unsplash": 45,   # free tier: 50/hr — leave 5 buffer
    "pexels": 180,    # free tier: 200/hr — leave 20 buffer
}

# Env-var overrides (optional)
_ENV_LIMIT_MAP: dict[str, str] = {
    "unsplash": "UNSPLASH_RATE_LIMIT",
    "pexels": "PEXELS_RATE_LIMIT",
}

# ── Singleton registry ──────────────────────────────────────────
_singletons: dict[str, "TokenBucketRateLimiter"] = {}
_singletons_lock = threading.Lock()


def _resolve_limit(provider_name: str) -> int:
    """Resolve rate limit: env override → default → 9999 (unlimited)."""
    env_var = _ENV_LIMIT_MAP.get(provider_name)
    if env_var:
        val = os.getenv(env_var, "").strip()
        if val and val.isdigit():
            return int(val)
    return DEFAULT_LIMITS.get(provider_name, 9999)


class TokenBucketRateLimiter:
    """Sliding-window token bucket that tracks requests over 1 hour.

    Usage:
        limiter = TokenBucketRateLimiter("unsplash", max_per_hour=45)
        for query in queries:
            limiter.wait_if_needed()
            provider.search(query)

    Gettable as a singleton via ``TokenBucketRateLimiter.get("unsplash")``
    so that multiple provider instances share the same counter.
    """

    @staticmethod
    def get(provider_name: str, max_per_hour: int | None = None) -> "TokenBucketRateLimiter":
        """Return the global singleton limiter for *provider_name*."""
        with _singletons_lock:
            if provider_name not in _singletons:
                _singletons[provider_name] = TokenBucketRateLimiter(
                    provider_name, max_per_hour=max_per_hour
                )
            return _singletons[provider_name]

    @staticmethod
    def reset_all() -> None:
        """Reset all singleton limiters (useful for tests)."""
        with _singletons_lock:
            _singletons.clear()

    def __init__(self, provider_name: str, max_per_hour: int | None = None) -> None:
        self._name = provider_name
        self._max_per_hour = max_per_hour if max_per_hour is not None else _resolve_limit(provider_name)
        self._window_seconds = 3600.0  # 1 hour sliding window
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

        if self._max_per_hour < 9999:
            logger.info(
                "Rate limiter [%s]: %d req/hour (%.1f req/min avg)",
                self._name, self._max_per_hour, self._max_per_hour / 60.0,
            )

    @property
    def max_per_hour(self) -> int:
        return self._max_per_hour

    @property
    def name(self) -> str:
        return self._name

    def try_acquire(self) -> bool:
        """Non-blocking check: return True if a slot is available (and record it).

        When False is returned the caller SHOULD skip this provider and
        try a fallback instead of calling ``wait_if_needed()`` which
        would block the entire pipeline.
        """
        if self._max_per_hour >= 9999:
            self.record()
            return True
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window_seconds
            self._timestamps = [ts for ts in self._timestamps if ts > cutoff]
            if len(self._timestamps) >= self._max_per_hour:
                logger.warning(
                    "Rate limiter [%s]: %d/%d exhausted — "
                    "returning False so caller can fall back",
                    self._name, len(self._timestamps), self._max_per_hour,
                )
                return False
            self._timestamps.append(now)
            return True

    def wait_if_needed(self) -> None:
        """Block until a request slot is available in the current window."""
        if self._max_per_hour >= 9999:
            return  # unlimited

        with self._lock:
            now = time.monotonic()
            # Evict timestamps older than 1 hour
            cutoff = now - self._window_seconds
            self._timestamps = [ts for ts in self._timestamps if ts > cutoff]

            if len(self._timestamps) >= self._max_per_hour:
                # Need to wait until the oldest request expires
                oldest = self._timestamps[0]
                wait = oldest + self._window_seconds - now + 1.0  # +1s safety margin
                if wait > 0:
                    logger.warning(
                        "Rate limiter [%s]: %d/%d requests in window — "
                        "sleeping %.1fs",
                        self._name, len(self._timestamps),
                        self._max_per_hour, wait,
                    )
                    time.sleep(wait)
                    # Re-calculate after sleep
                    now = time.monotonic()
                    cutoff = now - self._window_seconds
                    self._timestamps = [ts for ts in self._timestamps if ts > cutoff]

            self._timestamps.append(now)

    def record(self) -> None:
        """Record a request without blocking (for fire-and-forget tracking).

        Prefer ``wait_if_needed()`` in most cases; this is useful when you
        want to count the request but already handled throttling elsewhere.
        """
        if self._max_per_hour >= 9999:
            return
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window_seconds
            self._timestamps = [ts for ts in self._timestamps if ts > cutoff]
            self._timestamps.append(now)

    def remaining(self) -> int:
        """Return how many requests can still be made in the current window."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._window_seconds
            self._timestamps = [ts for ts in self._timestamps if ts > cutoff]
            return max(0, self._max_per_hour - len(self._timestamps))
