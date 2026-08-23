"""Tests for scraper error handling, circuit breaker, failure counters.

Run:  python3 -m pytest tests/test_scrape_errors.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from scrapers.base import (
    PermanentBlock, RateLimitError, APIUnavailable,
    AuthenticationError, ContentNotFound,
    record_scraper_failure, reset_scraper_counter,
    get_source_health, _failure_counts,
)


class TestScraperErrors:
    """Test custom exception types exist and are importable."""

    def test_exceptions_are_exception_subclasses(self):
        assert issubclass(PermanentBlock, Exception)
        assert issubclass(RateLimitError, Exception)
        assert issubclass(APIUnavailable, Exception)
        assert issubclass(AuthenticationError, Exception)
        assert issubclass(ContentNotFound, Exception)

    def test_permanent_block_instantiable(self):
        e = PermanentBlock("Blocked")
        assert "Blocked" in str(e)

    def test_rate_limit_instantiable(self):
        e = RateLimitError("Rate limited")
        assert "Rate limited" in str(e)


class TestFailureCounter:
    """Test record_scraper_failure / reset_scraper_counter."""

    def setup_method(self):
        _failure_counts.clear()

    def test_record_increments(self):
        record_scraper_failure("pullpush", "Timeout")
        assert _failure_counts["pullpush"]["failures"] == 1
        assert _failure_counts["pullpush"]["last_error"] == "Timeout"

    def test_degraded_at_10(self):
        for _ in range(10):
            record_scraper_failure("wikipedia", "Rate limit")
        assert _failure_counts["wikipedia"]["failures"] == 10
        assert _failure_counts["wikipedia"]["degraded"] is True

    def test_no_degrade_before_10(self):
        for _ in range(9):
            record_scraper_failure("atlas", "404")
        assert _failure_counts["atlas"]["degraded"] is False

    def test_reset_clears_counter(self):
        for _ in range(5):
            record_scraper_failure("source", "err")
        reset_scraper_counter("source")
        assert _failure_counts["source"]["failures"] == 0
        assert _failure_counts["source"]["degraded"] is False
        assert _failure_counts["source"]["last_error"] == ""

    def test_independent_sources(self):
        record_scraper_failure("A", "e1")
        record_scraper_failure("A", "e2")
        record_scraper_failure("B", "e1")
        assert _failure_counts["A"]["failures"] == 2
        assert _failure_counts["B"]["failures"] == 1

    def test_get_source_health_returns_dict(self):
        record_scraper_failure("pullpush", "timeout")
        health = get_source_health()
        assert "pullpush" in health
        assert health["pullpush"]["failures"] == 1
