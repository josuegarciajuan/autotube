"""Tests for RAM Governor — proactive memory management."""

import pytest
from unittest.mock import patch, MagicMock


class TestAvailableMB:
    """available_mb() should return MB or -1 on error."""

    def test_available_mb_returns_int(self):
        from pipeline.ram_governor import available_mb

        result = available_mb()
        assert isinstance(result, int)
        # On a real Linux system, should be > 0 or -1
        assert result >= -1

    def test_available_mb_handles_error(self):
        from pipeline.ram_governor import available_mb

        with patch("os.sysconf", side_effect=OSError("mock error")):
            result = available_mb()
            assert result == -1


class TestWaitForRAM:
    """wait_for_ram() should block until enough RAM or timeout."""

    def test_wait_for_ram_sufficient(self):
        from pipeline.ram_governor import wait_for_ram

        # With plenty of RAM, wait_for_ram returns immediately (True)
        with patch("pipeline.ram_governor.available_mb", return_value=5000):
            result = wait_for_ram(min_mb=3000, timeout_sec=1)
            assert result is True

    def test_wait_for_ram_insufficient_timeout(self):
        from pipeline.ram_governor import wait_for_ram

        # With insufficient RAM, wait_for_ram times out
        with patch("pipeline.ram_governor.available_mb", return_value=500):
            with patch("pipeline.ram_governor.time.sleep", return_value=None):
                result = wait_for_ram(min_mb=3000, timeout_sec=1)
                assert result is False

    def test_wait_for_ram_unknown(self):
        from pipeline.ram_governor import wait_for_ram

        # -1 means "can't determine" — should pass through
        with patch("pipeline.ram_governor.available_mb", return_value=-1):
            result = wait_for_ram(min_mb=3000, timeout_sec=1)
            assert result is False  # -1 is < 3000


class TestRecommendedThreads:
    """recommended_ffmpeg_threads() should scale with RAM."""

    def test_low_ram_returns_2(self):
        from pipeline.ram_governor import recommended_ffmpeg_threads

        with patch("pipeline.ram_governor.available_mb", return_value=2000):
            result = recommended_ffmpeg_threads()
            assert result == 2

    def test_medium_ram_returns_3(self):
        from pipeline.ram_governor import recommended_ffmpeg_threads

        with patch("pipeline.ram_governor.available_mb", return_value=5000):
            result = recommended_ffmpeg_threads()
            assert result == 3

    def test_high_ram_returns_4(self):
        from pipeline.ram_governor import recommended_ffmpeg_threads

        with patch("pipeline.ram_governor.available_mb", return_value=8000):
            result = recommended_ffmpeg_threads()
            assert result == 4

    def test_unknown_ram_returns_4(self):
        from pipeline.ram_governor import recommended_ffmpeg_threads

        with patch("pipeline.ram_governor.available_mb", return_value=-1):
            result = recommended_ffmpeg_threads()
            assert result == 4  # default


class TestRAMGates:
    """is_ram_ok_for_render and is_ram_ok_for_dispatch gates."""

    def test_render_gate_sufficient(self):
        from pipeline.ram_governor import is_ram_ok_for_render

        with patch("pipeline.ram_governor.available_mb", return_value=5000):
            assert is_ram_ok_for_render() is True

    def test_render_gate_insufficient(self):
        from pipeline.ram_governor import is_ram_ok_for_render

        with patch("pipeline.ram_governor.available_mb", return_value=1500):
            assert is_ram_ok_for_render() is False

    def test_dispatch_gate_sufficient(self):
        from pipeline.ram_governor import is_ram_ok_for_dispatch

        with patch("pipeline.ram_governor.available_mb", return_value=5000):
            assert is_ram_ok_for_dispatch() is True

    def test_dispatch_gate_insufficient(self):
        from pipeline.ram_governor import is_ram_ok_for_dispatch

        with patch("pipeline.ram_governor.available_mb", return_value=2500):
            assert is_ram_ok_for_dispatch() is False
