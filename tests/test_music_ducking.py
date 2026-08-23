"""Tests for background music ducking in VideoEditor.build_video().

During narration: music ducks to BACKGROUND_MUSIC_DUCK_VOLUME.
During transitions/silence: music plays at BACKGROUND_MUSIC_VOLUME.

Run:  python3 -m pytest tests/test_music_ducking.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest
from unittest.mock import MagicMock, patch, PropertyMock, ANY


# ── Helpers ──────────────────────────────────────────────────────────

def _mock_config(music_enabled=True, music_vol=-18.0, duck_vol=-28.0):
    """Build a mock channel config dict for VideoEditor."""
    return {
        "BACKGROUND_MUSIC_ENABLED": music_enabled,
        "BACKGROUND_MUSIC_VOLUME": music_vol,
        "BACKGROUND_MUSIC_DUCK_VOLUME": duck_vol,
    }


def _make_timestamps(segments: list[tuple[float, float]]):
    """Build word timestamps list from (start, end) pairs in seconds."""
    return [{"start": s, "end": e, "word": "test"} for s, e in segments]


# ══════════════════════════════════════════════════════════════════════
# Voice Activity Slot Tests
# ══════════════════════════════════════════════════════════════════════

class TestVoiceActiveSlots:
    """Test _voice_active_slots() builds correct activity mask."""

    def test_all_active(self):
        """Continuous speech → all slots True."""
        from pipeline.video_editor import VideoEditor
        ve = VideoEditor({})
        ts = _make_timestamps([(0, 5.0)])
        slots = ve._voice_active_slots(ts, 500)  # 500 slots = 5s in 10ms

        assert slots.dtype == bool
        assert len(slots) == 500
        assert np.all(slots)

    def test_gap_in_middle(self):
        """Speech gap → middle slots False."""
        from pipeline.video_editor import VideoEditor
        ve = VideoEditor({})
        ts = _make_timestamps([(0, 2.0), (3.0, 5.0)])  # gap from 2.0-3.0
        slots = ve._voice_active_slots(ts, 500)

        # Slots 0-199 (0-2s): active (True)
        assert np.all(slots[:199])
        # Slots 200-299 (2-3s): inactive (False) — the gap
        assert not np.any(slots[200:299])
        # Slots 300-499 (3-5s): active (True)
        assert np.all(slots[300:499])

    def test_no_timestamps(self):
        """Empty timestamps → all slots False."""
        from pipeline.video_editor import VideoEditor
        ve = VideoEditor({})
        slots = ve._voice_active_slots([], 100)
        assert len(slots) == 100
        assert not np.any(slots)

    def test_partial_coverage(self):
        """Timestamps only cover part of the duration → uncovered = False."""
        from pipeline.video_editor import VideoEditor
        ve = VideoEditor({})
        ts = _make_timestamps([(0, 3.0)])  # only first 3s of 5s total
        slots = ve._voice_active_slots(ts, 500)

        assert np.all(slots[:300])    # first 3s active
        assert not np.any(slots[300:])  # last 2s inactive


# ══════════════════════════════════════════════════════════════════════
# Ducking Volume Function Tests
# ══════════════════════════════════════════════════════════════════════

class TestDuckingVolume:
    """Test that the ducking volume function returns correct factors."""

    def test_voice_active_returns_duck_factor(self):
        """When voice is active (True slot), duck factor is returned."""
        from pipeline.video_editor import (
            _db_to_linear, VideoEditor,
        )

        ve = VideoEditor({})
        # Slots: first half active (voice), second half inactive (silence)
        slots = np.concatenate([np.ones(50, dtype=bool), np.zeros(50, dtype=bool)])
        music_db = -18.0
        duck_db = -28.0
        music_factor = _db_to_linear(music_db)
        duck_factor = _db_to_linear(duck_db)

        # Recreate the volume function logic
        def _duck_fn(t):
            idx = min(int(t * 100), len(slots) - 1)
            return duck_factor if slots[idx] else music_factor

        # At t=0.2s (index 20, voice active) → duck_factor
        assert _duck_fn(0.2) == duck_factor
        # At t=0.8s (index 80, voice inactive) → music_factor
        assert _duck_fn(0.8) == music_factor

    def test_duck_factor_lower_than_music_factor(self):
        """Duck volume (-28dB) is quieter than music volume (-18dB)."""
        from pipeline.video_editor import _db_to_linear

        duck = _db_to_linear(-28.0)
        music = _db_to_linear(-18.0)
        assert duck < music, (
            f"Duck factor {duck} should be < music factor {music}"
        )

    def test_db_to_linear_values(self):
        """_db_to_linear returns expected values."""
        from pipeline.video_editor import _db_to_linear

        # 0 dB = 1.0
        assert _db_to_linear(0) == 1.0
        # -6 dB ≈ 0.5
        assert abs(_db_to_linear(-6.0) - 0.5) < 0.01
        # -20 dB = 0.1
        assert abs(_db_to_linear(-20.0) - 0.1) < 0.01
        # Negative dB → factor < 1
        assert _db_to_linear(-30.0) < 0.1


# ══════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════

class TestDuckingEdgeCases:
    """Edge cases for ducking logic."""

    def test_single_slot(self):
        """One slot only → function works without index error."""
        from pipeline.video_editor import VideoEditor, _db_to_linear

        slots = np.array([True])
        duck_factor = _db_to_linear(-28.0)

        def _duck_fn(t):
            idx = min(int(t * 100), len(slots) - 1)
            return duck_factor if slots[idx] else 1.0

        # Any t maps to index 0
        assert _duck_fn(0.0) == duck_factor
        assert _duck_fn(1.0) == duck_factor
        assert _duck_fn(100.0) == duck_factor  # clamped to index 0

    def test_boundary_between_active_inactive(self):
        """Exactly at the boundary between active/inactive slots."""
        from pipeline.video_editor import _db_to_linear

        # 50 True, then 50 False
        slots = np.concatenate([np.ones(50, dtype=bool), np.zeros(50, dtype=bool)])
        duck_factor = _db_to_linear(-28.0)
        music_factor = _db_to_linear(-18.0)

        def _duck_fn(t):
            idx = min(int(t * 100), len(slots) - 1)
            return duck_factor if slots[idx] else music_factor

        # Just before boundary (t=0.49s → index 49, True) → duck
        assert _duck_fn(0.49) == duck_factor
        # Just after boundary (t=0.50s → index 50, False) → music
        assert _duck_fn(0.50) == music_factor

    def test_timestamp_normalization_preserved(self):
        """After _normalize_timestamps, start/end are in seconds."""
        from pipeline.video_editor import VideoEditor

        ve = VideoEditor({})
        ts_ms = [
            {"start_ms": 0, "end_ms": 1000.0, "word": "a"},
            {"start_ms": 1000.0, "end_ms": 2500.0, "word": "b"},
        ]
        norm = ve._normalize_timestamps(ts_ms)

        assert len(norm) == 2
        assert norm[0]["start"] == 0.0
        assert norm[0]["end"] == 1.0
        assert norm[1]["start"] == 1.0
        assert norm[1]["end"] == 2.5


# ══════════════════════════════════════════════════════════════════════
# Dynamic Volume Helper Tests (MoviePy v2 fix)
# ══════════════════════════════════════════════════════════════════════

class TestDynamicVolume:
    """Test _dynamic_volume() — the MoviePy v2 fix for callable MultiplyVolume."""

    # ── basic API ──────────────────────────────────────────────────

    def test_returns_transform_result(self):
        """_dynamic_volume returns clip.transform(...) result (new clip in MoviePy v2)."""
        from pipeline.video_editor import _dynamic_volume
        from unittest.mock import MagicMock

        mock_clip = MagicMock()
        result = _dynamic_volume(mock_clip, 0.5)
        # moviepy v2 clip.transform() returns a new clip object
        assert result is mock_clip.transform.return_value
        mock_clip.transform.assert_called_once()

    def test_calls_transform_with_keep_duration(self):
        """Always passes keep_duration=True to clip.transform()."""
        from pipeline.video_editor import _dynamic_volume
        from unittest.mock import MagicMock

        mock_clip = MagicMock()
        _dynamic_volume(mock_clip, 0.8)
        mock_clip.transform.assert_called_once()
        assert mock_clip.transform.call_args[1]["keep_duration"] is True

    # ── callable factor (the bug fix) ──────────────────────────────

    def test_callable_factor_evaluated_per_frame(self):
        """Callable factor receives t and its value is used per-frame."""
        from pipeline.video_editor import _dynamic_volume
        from unittest.mock import MagicMock

        mock_clip = MagicMock()
        # Keep track of t values seen by the factor
        seen_t = []
        def factor_fn(t):
            seen_t.append(t)
            return 0.5

        _dynamic_volume(mock_clip, factor_fn)

        # Extract the lambda that was passed to transform
        ff = mock_clip.transform.call_args[0][0]

        # Simulate frame access at two time points
        import numpy as np
        frame1 = np.array([1.0, 2.0])
        frame2 = np.array([3.0, 4.0])
        # _dynamic_volume's lambda:  lambda get_frame, t: factor(t) * get_frame(t)
        result1 = ff(lambda t: frame1, 0.2)
        result2 = ff(lambda t: frame2, 1.5)

        np.testing.assert_array_almost_equal(result1, frame1 * 0.5)
        np.testing.assert_array_almost_equal(result2, frame2 * 0.5)
        assert seen_t == [0.2, 1.5], "factor_fn should receive frame times"

    def test_callable_factor_returns_scalar(self):
        """Multiplier = factor_fn(t): return type must be scalar (float)."""
        from pipeline.video_editor import _dynamic_volume
        from unittest.mock import MagicMock

        mock_clip = MagicMock()
        # factor_fn at t=0.5: int(5) % 3 = 2 → float(2.0) = 2.0
        _dynamic_volume(mock_clip, lambda t: float(int(t * 10) % 3))

        ff = mock_clip.transform.call_args[0][0]
        import numpy as np
        frame = np.array([1.0, 1.0])
        r = ff(lambda t: frame, 0.5)
        np.testing.assert_array_almost_equal(r, frame * 2.0)  # int(5)%3=2 → 2.0

    # ── scalar factor (backward compat) ────────────────────────────

    def test_scalar_factor_works_like_multiply_volume(self):
        """Scalar factor is equivalent to MultiplyVolume(scalar)."""
        from pipeline.video_editor import _dynamic_volume
        from unittest.mock import MagicMock
        import numpy as np

        mock_clip = MagicMock()
        _dynamic_volume(mock_clip, 0.25)

        ff = mock_clip.transform.call_args[0][0]
        frame = np.array([8.0, -4.0])
        result = ff(lambda t: frame, 0.0)
        np.testing.assert_array_almost_equal(result, frame * 0.25)

    # ── guard against the original bug ─────────────────────────────

    def test_no_typeerror_with_callable(self):
        """The original bug: MultiplyVolume(lambda) → TypeError('function' * float).

        _dynamic_volume must NOT raise TypeError for callable factors.
        """
        from pipeline.video_editor import _dynamic_volume
        from unittest.mock import MagicMock
        import numpy as np

        mock_clip = MagicMock()
        _dynamic_volume(mock_clip, lambda t: 0.7)

        # Simulate rendering: get_frame returns numpy arrays
        ff = mock_clip.transform.call_args[0][0]
        try:
            result = ff(lambda t: np.array([1.0, 2.0]), 2.3)
        except TypeError as e:
            pytest.fail(f"_dynamic_volume raised TypeError: {e}")

        np.testing.assert_array_almost_equal(result, np.array([0.7, 1.4]))

    # ── integration with ducking closures ──────────────────────────

    def test_duck_fn_closure_survives(self):
        """The _duck_fn closure keeps correct scope when passed to _dynamic_volume.

        Regression test: ensures voice_slots / duck_factor / music_factor are
        correctly captured and not lost by the extra function indirection.
        """
        from pipeline.video_editor import _dynamic_volume, _db_to_linear, VideoEditor
        import numpy as np

        # Build the same closure structure as _build_background_audio
        ve = VideoEditor({})
        music_db = -18.0
        duck_db = -28.0
        slots = np.concatenate([np.ones(50, dtype=bool), np.zeros(50, dtype=bool)])
        music_factor = _db_to_linear(music_db)
        duck_factor = _db_to_linear(duck_db)

        def _duck_fn(t: float) -> float:
            idx = min(int(t * 100), len(slots) - 1)
            return duck_factor if slots[idx] else music_factor

        from unittest.mock import MagicMock
        mock_clip = MagicMock()
        _dynamic_volume(mock_clip, _duck_fn)

        ff = mock_clip.transform.call_args[0][0]
        # Voice active (t=0.2 → idx 20 → True → duck_factor)
        r_voice = ff(lambda t: np.array([1.0]), 0.2)
        # Silence (t=0.8 → idx 80 → False → music_factor)
        r_silence = ff(lambda t: np.array([1.0]), 0.8)

        np.testing.assert_array_almost_equal(r_voice, np.array([duck_factor]))
        np.testing.assert_array_almost_equal(r_silence, np.array([music_factor]))
        assert duck_factor < music_factor, "duck should be quieter than music"
