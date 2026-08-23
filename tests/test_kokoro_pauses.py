"""Tests for KokoroTTS pause-at-paragraph-boundaries logic.

Ensures pauses are only inserted at paragraph boundaries
(is_last_in_paragraph=True), not between every block.

Run:  python3 -m pytest tests/test_kokoro_pauses.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest
from unittest.mock import MagicMock


# ── Helpers ──────────────────────────────────────────────────────────

def _make_bloque(tipo="desarrollo", texto="Test text.", paragraph_idx=0,
                 is_last=False):
    """Build a single block dict."""
    return {
        "tipo": tipo,
        "texto": texto,
        "paragraph_idx": paragraph_idx,
        "is_last_in_paragraph": is_last,
    }


def _make_mock_pipeline(audio_len_samples=24000):
    """Return a mock KPipeline whose __call__ yields one audio chunk."""

    def _generator(text, voice, speed):
        yield (None, None, np.zeros(audio_len_samples, dtype=np.float32))

    mock = MagicMock()
    mock.side_effect = _generator
    return mock


# ══════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════

class TestKokoroPauses:
    """Verify pause insertion logic in generate_segmented()."""

    def test_no_pause_before_first_block(self):
        """First block should NOT have a pause before it."""
        from pipeline.kokoro_tts import KokoroTTSEngine

        engine = KokoroTTSEngine({
            "kokoro_voice": "em_alex",
            "pause_between_blocks": 0.8,
            "block_speeds": {"hook": 1.0, "desarrollo": 1.0},
        })

        # Set the pipeline directly (bypass lazy init)
        engine._pipeline = _make_mock_pipeline(audio_len_samples=24000 * 2)
        engine._pipeline._pipeline = engine._pipeline  # for property access

        bloques = [
            _make_bloque("hook", "Primer bloque.", paragraph_idx=0, is_last=False),
            _make_bloque("desarrollo", "Segundo bloque.", paragraph_idx=0, is_last=True),
        ]

        audio_path, timestamps = engine.generate_segmented(bloques)

        # 2 blocks × 2s audio = 4s. No pause before first block.
        total_dur = timestamps[-1]["end_ms"] / 1000.0 if timestamps else 0
        assert 4.0 <= total_dur <= 4.5, (
            f"Expected ~4.0s (no pause before first block), got {total_dur}s"
        )

    def test_no_pause_within_paragraph(self):
        """Blocks in the SAME paragraph should NOT have a pause between them."""
        from pipeline.kokoro_tts import KokoroTTSEngine

        engine = KokoroTTSEngine({
            "kokoro_voice": "em_alex",
            "pause_between_blocks": 0.8,
            "block_speeds": {"hook": 1.0, "desarrollo": 1.0, "climax": 1.0},
        })

        engine._pipeline = _make_mock_pipeline(audio_len_samples=24000 * 1)

        # 3 blocks, all same paragraph, none is_last in between
        bloques = [
            _make_bloque("hook", "A.", paragraph_idx=0, is_last=False),
            _make_bloque("desarrollo", "B.", paragraph_idx=0, is_last=False),
            _make_bloque("climax", "C.", paragraph_idx=0, is_last=True),
        ]

        audio_path, timestamps = engine.generate_segmented(bloques)

        # 3 blocks × 1s = 3s. No intra-paragraph pauses.
        total_dur = timestamps[-1]["end_ms"] / 1000.0 if timestamps else 0
        assert 2.9 <= total_dur <= 3.3, (
            f"Expected ~3.0s (no intra-pause), got {total_dur}s"
        )

    def test_pause_at_paragraph_boundary(self):
        """Pause IS inserted when previous block is is_last_in_paragraph=True."""
        from pipeline.kokoro_tts import KokoroTTSEngine

        pause_s = 0.8
        engine = KokoroTTSEngine({
            "kokoro_voice": "em_alex",
            "pause_between_blocks": pause_s,
            "block_speeds": {"hook": 1.0, "desarrollo": 1.0},
        })

        engine._pipeline = _make_mock_pipeline(audio_len_samples=24000 * 2)

        # Para 0 ends at block 0, Para 1 starts at block 1
        bloques = [
            _make_bloque("hook", "P0 block.", paragraph_idx=0, is_last=True),
            _make_bloque("desarrollo", "P1 block.", paragraph_idx=1, is_last=False),
        ]

        audio_path, timestamps = engine.generate_segmented(bloques)

        # 2 blocks × 2s + 1 pause × 0.8s = 4.8s
        total_dur = timestamps[-1]["end_ms"] / 1000.0 if timestamps else 0
        expected = 4.0 + pause_s  # 4.8
        assert expected - 0.1 <= total_dur <= expected + 0.3, (
            f"Expected ~{expected}s (with paragraph-boundary pause), got {total_dur}s"
        )

    def test_pause_zero_disabled(self):
        """pause_between_blocks=0 → no pauses anywhere."""
        from pipeline.kokoro_tts import KokoroTTSEngine

        engine = KokoroTTSEngine({
            "kokoro_voice": "em_alex",
            "pause_between_blocks": 0.0,  # disabled
            "block_speeds": {"hook": 1.0, "desarrollo": 1.0},
        })

        engine._pipeline = _make_mock_pipeline(audio_len_samples=24000 * 1)

        bloques = [
            _make_bloque("hook", "P0.", paragraph_idx=0, is_last=True),
            _make_bloque("desarrollo", "P1.", paragraph_idx=1, is_last=False),
        ]

        audio_path, timestamps = engine.generate_segmented(bloques)

        total_dur = timestamps[-1]["end_ms"] / 1000.0 if timestamps else 0
        # 2 blocks × 1s = 2.0s, no pauses
        assert 1.9 <= total_dur <= 2.2, (
            f"Expected ~2.0s (pauses disabled), got {total_dur}s"
        )

    def test_multiple_paragraphs(self):
        """Correct pauses across 3 paragraphs."""
        from pipeline.kokoro_tts import KokoroTTSEngine

        pause_s = 0.7
        engine = KokoroTTSEngine({
            "kokoro_voice": "em_alex",
            "pause_between_blocks": pause_s,
            "block_speeds": {"hook": 1.0, "desarrollo": 1.0},
        })

        engine._pipeline = _make_mock_pipeline(audio_len_samples=24000 * 1)

        # 3 paragraphs, 2 boundaries → 2 pauses
        bloques = [
            _make_bloque("hook", "P0.", paragraph_idx=0, is_last=True),
            _make_bloque("desarrollo", "P1.", paragraph_idx=1, is_last=True),
            _make_bloque("desarrollo", "P2.", paragraph_idx=2, is_last=True),
        ]

        audio_path, timestamps = engine.generate_segmented(bloques)

        # 3 blocks × 1s + 2 pauses × 0.7s = 4.4s
        total_dur = timestamps[-1]["end_ms"] / 1000.0 if timestamps else 0
        expected = 3.0 + 2 * pause_s  # 4.4
        assert expected - 0.15 <= total_dur <= expected + 0.25, (
            f"Expected ~{expected}s (3 blocks + 2 paragraph pauses), got {total_dur}s"
        )
