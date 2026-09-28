"""Tests for _compute_word_target() and _get_word_target().

Run:  python3 -m pytest tests/test_word_target.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from types import SimpleNamespace
from unittest.mock import patch
from pipeline.script_generator import ScriptGenerator
from pipeline.video_validator import VideoValidator
from tests.conftest import MockDB, MockConfigCanal2, MockConfigCanal3, MockConfigKokoro


class TestComputeWordTarget:
    """Test _compute_word_target() with real channel configs."""

    def test_canal2_production(self):
        """14 min, -10% rate → 2426 palabras (165 wpm × 1.05 colchón)."""
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        wt = sg._compute_word_target(14.0)
        assert wt["palabras_objetivo"] == 2426
        assert wt["words_min"] == 2062   # 2426 * 0.85 = 2062.1
        assert wt["words_max"] == 3153   # 2426 * 1.3 = 3153.8
        assert wt["duration_target"] == 14.0
        assert wt["blocks_min"] >= 3
        assert wt["blocks_max"] >= 5

    def test_canal3_production(self):
        """12 min, -8% rate."""
        sg = ScriptGenerator(MockDB(), MockConfigCanal3)
        wt = sg._compute_word_target(12.0)
        # 12 × 150 × 1.08 × 1.05 = 2041.2 → 2042
        assert 1950 <= wt["palabras_objetivo"] <= 2150
        assert wt["words_min"] > 0
        assert wt["duration_target"] == 12.0

    def test_kokoro_production(self):
        """14 min, 0.85 speed (direct multiplier = 85% of neutral)."""
        sg = ScriptGenerator(MockDB(), MockConfigKokoro)
        wt = sg._compute_word_target(14.0)
        # 14 × 150 × 0.85 × 1.05 = 1874.25 → 1875
        assert 1800 <= wt["palabras_objetivo"] <= 1950

    def test_small_target(self):
        """Even 1-minute target works."""
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        wt = sg._compute_word_target(1.0)
        assert wt["palabras_objetivo"] >= 50
        assert wt["words_min"] >= 100

    def test_palabras_objetivo_always_present(self):
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        wt = sg._compute_word_target(10.0)
        assert "palabras_objetivo" in wt

    def test_words_min_never_below_100(self):
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        wt = sg._compute_word_target(0.1)
        assert wt["words_min"] >= 100, f"Got words_min={wt['words_min']}"

    def test_blocks_positive(self):
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        wt = sg._compute_word_target(10.0)
        assert wt["blocks_min"] >= 3
        assert wt["blocks_max"] >= 5


class TestGetWordTarget:
    """Test _get_word_target() which adds random variation."""

    def test_random_range(self):
        """100 runs should all be within [mean - disc, mean + disc]."""
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        for _ in range(100):
            wt = sg._get_word_target()
            assert 11 <= wt["duration_target"] <= 17, \
                f"duration_target={wt['duration_target']} outside [11,17]"
            assert wt["palabras_objetivo"] > 0

    def test_uses_voice_timing(self):
        """_get_word_target calls _compute_word_target under the hood."""
        sg = ScriptGenerator(MockDB(), MockConfigCanal2)
        wt = sg._get_word_target()
        assert "palabras_objetivo" in wt
        assert "words_min" in wt
        assert "words_max" in wt
        assert "duration_target" in wt

    def test_objective_wins_over_legacy_prod_max(self):
        """Regression: PROD_VIDEO_DURATION_MAX must NOT clamp the panel objective.

        Before the fix, a channel with objective 16 ± 3 and legacy
        PROD_VIDEO_DURATION_MAX=9 always generated ~9 min scripts.
        """
        cfg = SimpleNamespace(
            CANAL_NAME="canalX",
            TEST_MODE=False,
            VIDEO_AVERAGE_DURATION_MIN=16,
            VIDEO_DURATION_DISCREPANCY_MIN=3,
            PROD_VIDEO_DURATION_MAX=9,
            TTS_STRATEGY={"rate_base": "-10%"},
        )
        sg = ScriptGenerator(MockDB(), cfg)
        for _ in range(100):
            wt = sg._get_word_target()
            assert 13 <= wt["duration_target"] <= 19, \
                f"objective clamped: duration_target={wt['duration_target']}"

    def test_objective_handles_bad_values(self):
        """Non-numeric objective falls back to sane defaults instead of crashing."""
        cfg = SimpleNamespace(
            CANAL_NAME="canalX",
            TEST_MODE=False,
            VIDEO_AVERAGE_DURATION_MIN="oops",
            VIDEO_DURATION_DISCREPANCY_MIN=None,
            TTS_STRATEGY={"rate_base": "-10%"},
        )
        sg = ScriptGenerator(MockDB(), cfg)
        wt = sg._get_word_target()
        assert wt["duration_target"] > 0
        assert wt["palabras_objetivo"] > 0


class TestValidatorDurationRange:
    """VideoValidator derives its duration warning range from the objective."""

    def test_range_derived_from_objective(self):
        cfg = SimpleNamespace(
            TITLE_POWER_WORDS=[],
            VIDEO_AVERAGE_DURATION_MIN=16,
            VIDEO_DURATION_DISCREPANCY_MIN=3,
            PROD_VIDEO_DURATION_MIN=6,
            PROD_VIDEO_DURATION_MAX=9,
        )
        v = VideoValidator(cfg)
        assert v.duration_min_sec == 13 * 60
        assert v.duration_max_sec == 19 * 60

    def test_falls_back_to_legacy_when_no_objective(self):
        cfg = SimpleNamespace(
            TITLE_POWER_WORDS=[],
            PROD_VIDEO_DURATION_MIN=6,
            PROD_VIDEO_DURATION_MAX=9,
        )
        v = VideoValidator(cfg)
        assert v.duration_min_sec == 6 * 60
        assert v.duration_max_sec == 9 * 60
