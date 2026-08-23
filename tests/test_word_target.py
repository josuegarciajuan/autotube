"""Tests for _compute_word_target() and _get_word_target().

Run:  python3 -m pytest tests/test_word_target.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import patch
from pipeline.script_generator import ScriptGenerator
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
