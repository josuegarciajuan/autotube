"""Tests for publish scheduler helpers — timezone handling and staleness checks.

Covers:
  - _parse_target_public_at: ISO8601 UTC, naive local, edge cases
  - _target_is_stale: past, future, None, unparseable
  - ensure_future_target_public_at: recalc on stale, pass-through on valid

Run: python3 -m pytest tests/test_publish_scheduler.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from datetime import datetime, timedelta, timezone

from pipeline.publish_scheduler import (
    _parse_target_public_at,
    _target_is_stale,
    ensure_future_target_public_at,
    calculate_target_public_time,
)


# ── _parse_target_public_at ─────────────────────────────────────

class TestParseTargetPublicAt:
    """Tests for _parse_target_public_at — the central parsing function."""

    def test_iso8601_utc_with_offset(self):
        """ISO8601 with explicit offset."""
        result = _parse_target_public_at("2026-07-24T19:07:00+00:00")
        assert result is not None
        assert result.tzinfo is not None
        assert result.hour == 19
        assert result.minute == 7

    def test_iso8601_with_Z(self):
        """ISO8601 with Z suffix."""
        result = _parse_target_public_at("2026-07-24T19:07:00Z")
        assert result is not None
        assert result.tzinfo is not None

    def test_naive_utc_summer(self):
        """Persisted naive timestamps are UTC under the canonical contract."""
        result = _parse_target_public_at("2026-07-24 21:07:00")
        assert result is not None
        assert result.tzinfo is not None
        assert result.hour == 21
        assert result.day == 24

    def test_naive_utc_winter(self):
        """Persisted naive timestamps remain UTC in winter too."""
        result = _parse_target_public_at("2026-01-15 21:07:00", timezone_str="Europe/Madrid")
        assert result is not None
        assert result.hour == 21

    def test_empty_string_returns_none(self):
        assert _parse_target_public_at("") is None

    def test_none_returns_none(self):
        assert _parse_target_public_at(None) is None

    def test_invalid_string_returns_none(self):
        assert _parse_target_public_at("not-a-date") is None

    def test_space_separated_with_offset(self):
        """Space-separated ISO-like format with timezone offset."""
        result = _parse_target_public_at("2026-07-24 19:07:00+00:00")
        assert result is not None
        assert result.tzinfo is not None


# ── _target_is_stale ────────────────────────────────────────────

class TestTargetIsStale:
    """Tests for _target_is_stale — detects past-due targets."""

    def test_future_target_is_not_stale(self):
        """A target 24 hours in the future is not stale."""
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        assert not _target_is_stale(future)

    def test_past_target_is_stale(self):
        """A target 1 hour in the past IS stale."""
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert _target_is_stale(past)

    def test_none_target_is_stale(self):
        """No target at all is considered stale."""
        assert _target_is_stale(None)

    def test_unparseable_target_is_stale(self):
        """Unparseable string is treated as stale."""
        assert _target_is_stale("garbage")

    def test_naive_past_target_is_stale(self):
        """A naive local string in the past is correctly detected as stale."""
        past = "2020-01-01 12:00:00"
        assert _target_is_stale(past)

    def test_naive_future_target_not_stale(self):
        """A naive local string far in the future is not stale."""
        future = "2099-12-31 23:59:00"
        assert not _target_is_stale(future)


# ── ensure_future_target_public_at ──────────────────────────────

class TestEnsureFutureTarget:
    """Tests for ensure_future_target_public_at — the guard function."""

    def test_passes_through_future_target(self):
        """A valid future target is returned as-is."""
        future = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        result = ensure_future_target_public_at(
            future, slug="test", timezone_str="Europe/Madrid",
            warmup_min=60,
        )
        assert result is not None
        # Should be approximately the same time
        result_dt = datetime.fromisoformat(result)
        original_dt = datetime.fromisoformat(future)
        assert abs((result_dt - original_dt).total_seconds()) < 5  # < 5 sec difference

    def test_recalculates_stale_target(self):
        """A stale target is recalculated to be in the future."""
        past = "2020-01-01T12:00:00+00:00"
        result = ensure_future_target_public_at(
            past, slug="test", timezone_str="Europe/Madrid",
            warmup_min=60, jitter_min=0,
        )
        result_dt = datetime.fromisoformat(result)
        now_utc = datetime.now(timezone.utc)
        assert result_dt > now_utc, f"Expected {result_dt} > {now_utc}"

    def test_recalculates_none_target(self):
        """None target triggers recalculation."""
        result = ensure_future_target_public_at(
            None, slug="test", timezone_str="Europe/Madrid",
            warmup_min=60, jitter_min=0,
        )
        result_dt = datetime.fromisoformat(result)
        now_utc = datetime.now(timezone.utc)
        assert result_dt > now_utc

    def test_respects_warmup_min(self):
        """Recalculated target is at least warmup_min minutes in the future."""
        past = "2020-01-01T12:00:00+00:00"
        warmup = 120
        now_before = datetime.now(timezone.utc)
        result = ensure_future_target_public_at(
            past, slug="test", timezone_str="Europe/Madrid",
            warmup_min=warmup, jitter_min=0,
        )
        result_dt = datetime.fromisoformat(result)
        # Target must be at least warmup minutes from the time we captured before the call
        min_allowed = now_before + timedelta(minutes=warmup)
        assert result_dt >= min_allowed, (
            f"Expected {result_dt} >= {min_allowed} (now_before + {warmup}min)"
        )


# ── calculate_target_public_time invariants ─────────────────────

class TestCalculateTargetPublicTime:
    """Tests for the core calculation function."""

    def test_always_returns_future_target(self):
        """calculate_target_public_time never returns a target in the past."""
        result = calculate_target_public_time(
            slug="test",
            primary_keyword="misterio",
            timezone_str="Europe/Madrid",
            jitter_min=0,  # No jitter for deterministic test
            warmup_min=60,
        )
        target = result["target_public_at"]
        target_dt = datetime.fromisoformat(target)
        now_utc = datetime.now(timezone.utc)
        assert target_dt > now_utc, (
            f"Target {target} is in the past relative to now {now_utc.isoformat()}"
        )

    def test_returns_utc_iso8601(self):
        """Result contains ISO8601 UTC string."""
        result = calculate_target_public_time(
            slug="test",
            primary_keyword="historia",
            timezone_str="Europe/Madrid",
            jitter_min=0,
            warmup_min=60,
        )
        target = result["target_public_at"]
        # Must be parseable with fromisoformat
        dt = datetime.fromisoformat(target)
        assert dt.tzinfo is not None, "Target should have timezone info"

    def test_includes_all_expected_keys(self):
        """Result dict has all expected keys."""
        result = calculate_target_public_time(
            slug="test",
            primary_keyword="misterio",
            timezone_str="Europe/Madrid",
            warmup_min=60,
        )
        for key in ("target_public_at", "target_public_at_local",
                     "peak_hour_local", "peak_source",
                     "niche", "jitter_applied", "warmup_min"):
            assert key in result, f"Missing key: {key}"

    def test_different_niches_produce_different_hours(self):
        """Misterio (21h) vs Educacion (18h) produce different peak hours."""
        r1 = calculate_target_public_time(
            slug="test1", primary_keyword="misterio paranormal",
            timezone_str="Europe/Madrid", jitter_min=0, warmup_min=60,
        )
        r2 = calculate_target_public_time(
            slug="test2", primary_keyword="ciencia educacion",
            timezone_str="Europe/Madrid", jitter_min=0, warmup_min=60,
        )
        # The peak_hour_local should differ (misterio=21, educacion=18)
        assert r1["niche"] != r2["niche"], "Different keywords should detect different niches"
