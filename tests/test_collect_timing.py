"""Tests for PipelineOrchestrator timing collection (collect_timing / collect_timing_json).

Run:  python3 -m pytest tests/test_collect_timing.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

import json
import time
import pytest
from unittest.mock import MagicMock, patch
from tests.conftest import MockDB


class TestCollectTiming:
    """Test PipelineOrchestrator.collect_timing() and collect_timing_json()."""

    def _make_orchestrator(self):
        """Create a minimally-mocked orchestrator that exposes timing internals."""
        from orchestrator import PipelineOrchestrator

        db = MockDB()
        with patch("database.db.Database", return_value=db):
            with patch("database.db.init_db"):
                orch = PipelineOrchestrator(canal="canal2", db_video_id=1)
        return orch, db

    # ── structure / format tests ──────────────────────────────────

    def test_collect_timing_returns_correct_structure(self):
        """collect_timing() returns a dict with 'phases' and 'total_duration_ms'."""
        orch, _ = self._make_orchestrator()
        result = orch.collect_timing()

        assert isinstance(result, dict)
        assert "phases" in result
        assert "total_duration_ms" in result
        assert isinstance(result["phases"], dict)
        assert isinstance(result["total_duration_ms"], int)

    def test_collect_timing_json_is_valid_json(self):
        """collect_timing_json() returns a parseable JSON string matching collect_timing()."""
        orch, _ = self._make_orchestrator()

        json_str = orch.collect_timing_json()
        parsed = json.loads(json_str)

        assert "phases" in parsed
        assert "total_duration_ms" in parsed
        assert parsed["phases"] == orch.collect_timing()["phases"]
        assert parsed["total_duration_ms"] == orch.collect_timing()["total_duration_ms"]

    # ── _pipeline_start behaviour ─────────────────────────────────

    def test_pipeline_start_is_set_on_init(self):
        """_pipeline_start is not None after __init__ — wall-clock is tracked."""
        orch, _ = self._make_orchestrator()
        assert orch._pipeline_start is not None

    def test_total_duration_ms_uses_wall_clock(self):
        """total_duration_ms grows as real time passes (wall-clock, not phase sum)."""
        orch, _ = self._make_orchestrator()

        t1 = orch.collect_timing()["total_duration_ms"]

        # Wait a measurable amount
        time.sleep(0.05)

        t2 = orch.collect_timing()["total_duration_ms"]
        assert t2 > t1, f"total_duration_ms should increase over time ({t1} → {t2})"

    def test_total_duration_ms_with_null_start_falls_back(self):
        """If _pipeline_start is None, total = sum(phases)."""
        orch, _ = self._make_orchestrator()
        orch._pipeline_start = None

        # No phases → total should be 0
        result = orch.collect_timing()
        assert result["total_duration_ms"] == 0

    def test_total_duration_ms_reflects_only_wall_clock_not_phase_sum(self):
        """total_duration_ms comes from wall-clock, not from summing phases.

        Even when phases are manually injected with long durations,
        total_duration_ms should not jump — it reflects real elapsed time.
        """
        orch, _ = self._make_orchestrator()

        # Fake enormous phase durations
        orch._timing["phases"]["scrape"] = 999_999  # ~16 min
        orch._timing["phases"]["script"] = 888_888

        result = orch.collect_timing()
        # total should be very small (orchestrator just created), not ~1.8M
        assert result["total_duration_ms"] < 5000, (
            f"Expected wall-clock < 5s, got {result['total_duration_ms']}ms"
        )

    # ── phase accumulation ────────────────────────────────────────

    def test_phase_timings_accumulate_in_dict(self):
        """Manually added phase timings appear in collect_timing()."""
        orch, _ = self._make_orchestrator()

        orch._timing["phases"]["scrape"] = 1234
        orch._timing["phases"]["script"] = 2345
        orch._timing["phases"]["tts"] = 5678

        result = orch.collect_timing()
        assert result["phases"]["scrape"] == 1234
        assert result["phases"]["script"] == 2345
        assert result["phases"]["tts"] == 5678

    def test_empty_phases_returns_empty_dict(self):
        """With no phases recorded, phases is an empty dict."""
        orch, _ = self._make_orchestrator()

        result = orch.collect_timing()
        assert result["phases"] == {}

    def test_collect_timing_is_idempotent(self):
        """Calling collect_timing() twice returns the same phase data
        (but total_duration_ms may increase due to wall-clock)."""
        orch, _ = self._make_orchestrator()
        orch._timing["phases"]["media"] = 5000

        r1 = orch.collect_timing()
        r2 = orch.collect_timing()

        assert r1["phases"] == r2["phases"]
        assert r2["total_duration_ms"] >= r1["total_duration_ms"]

    # ── timing reset / mutability ─────────────────────────────────

    def test_collect_timing_returns_copy_not_reference(self):
        """Modifying the returned dict should not affect internal _timing."""
        orch, _ = self._make_orchestrator()
        orch._timing["phases"]["video_assembly"] = 7777

        result = orch.collect_timing()
        result["phases"]["video_assembly"] = 0  # mutate the returned dict

        # Internal timing should be preserved
        assert orch._timing["phases"]["video_assembly"] == 7777
