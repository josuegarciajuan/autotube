"""Tests for rate_limiter.py — singleton pattern, try_acquire, non-blocking behaviour.

Run:  python3 -m pytest tests/test_rate_limiter.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
import pytest
from unittest.mock import patch

from pipeline.rate_limiter import TokenBucketRateLimiter, _singletons


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset all singleton rate limiters before each test."""
    TokenBucketRateLimiter.reset_all()
    yield
    TokenBucketRateLimiter.reset_all()


class TestSingleton:
    """TokenBucketRateLimiter.get() should return the same instance."""

    def test_get_returns_same_instance(self):
        a = TokenBucketRateLimiter.get("unsplash")
        b = TokenBucketRateLimiter.get("unsplash")
        assert a is b, "get() should return singleton per provider name"

    def test_different_providers_different_instances(self):
        unsplash = TokenBucketRateLimiter.get("unsplash")
        pexels = TokenBucketRateLimiter.get("pexels")
        assert unsplash is not pexels, "different providers should have separate limiters"

    def test_reset_all_clears_singletons(self):
        a = TokenBucketRateLimiter.get("unsplash")
        TokenBucketRateLimiter.reset_all()
        b = TokenBucketRateLimiter.get("unsplash")
        assert a is not b, "reset_all should create a new instance"


class TestTryAcquire:
    """try_acquire() is non-blocking — returns True/False immediately."""

    def test_try_acquire_succeeds_under_limit(self):
        limiter = TokenBucketRateLimiter("test", max_per_hour=100)
        start = time.monotonic()
        result = limiter.try_acquire()
        elapsed = time.monotonic() - start
        assert result is True
        assert elapsed < 0.1, f"try_acquire should not block: {elapsed:.3f}s"

    def test_try_acquire_returns_false_when_exhausted(self):
        limiter = TokenBucketRateLimiter("test", max_per_hour=3)
        # Exhaust all slots
        assert limiter.try_acquire() is True
        assert limiter.try_acquire() is True
        assert limiter.try_acquire() is True
        # Now it should return False
        start = time.monotonic()
        result = limiter.try_acquire()
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 0.1, f"try_acquire should NOT block when exhausted: {elapsed:.3f}s"

    def test_try_acquire_does_not_sleep(self):
        limiter = TokenBucketRateLimiter("test", max_per_hour=1)
        limiter.try_acquire()  # exhaust
        with patch("pipeline.rate_limiter.time.sleep") as mock_sleep:
            result = limiter.try_acquire()
        assert result is False
        mock_sleep.assert_not_called()

    def test_wait_if_needed_still_works(self):
        """wait_if_needed() keeps its blocking behaviour (for direct use)."""
        limiter = TokenBucketRateLimiter("test", max_per_hour=1)
        limiter.try_acquire()  # exhaust
        with patch("pipeline.rate_limiter.time.sleep") as mock_sleep:
            limiter.wait_if_needed()
        # wait_if_needed should detect the exhausted limiter and sleep
        assert mock_sleep.call_count >= 1


class TestRemaining:
    """remaining() should report accurate counts."""

    def test_remaining_starts_at_max(self):
        limiter = TokenBucketRateLimiter("test", max_per_hour=50)
        assert limiter.remaining() == 50

    def test_remaining_decreases(self):
        limiter = TokenBucketRateLimiter("test", max_per_hour=10)
        for i in range(3):
            limiter.try_acquire()
        assert limiter.remaining() == 7

    def test_remaining_never_negative(self):
        limiter = TokenBucketRateLimiter("test", max_per_hour=2)
        limiter.try_acquire()
        limiter.try_acquire()
        limiter.try_acquire()  # exhausted
        assert limiter.remaining() == 0
        limiter.try_acquire()  # still exhausted
        assert limiter.remaining() == 0


class TestRecord:
    """record() should count without blocking."""

    def test_record_increments_count(self):
        limiter = TokenBucketRateLimiter("test", max_per_hour=100)
        before = limiter.remaining()
        limiter.record()
        assert limiter.remaining() == before - 1

    def test_record_does_not_block_when_exhausted(self):
        limiter = TokenBucketRateLimiter("test", max_per_hour=1)
        limiter.try_acquire()  # exhaust
        start = time.monotonic()
        limiter.record()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, "record() should never block"
