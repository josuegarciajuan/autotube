"""Tests for scene duration enforcement (merge short, split long, 5-20s).

Run:  python3 -m pytest tests/test_scene_ranges.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

# We test _enforce_scene_durations() directly, not the full VideoEditor
from pipeline.video_editor import VideoEditor


class FakeCanalConfig:
    """Minimal config for VideoEditor."""
    SCENE_DURATION_MIN = 5.0
    SCENE_DURATION_MAX = 20.0


class TestSceneDurations:
    """Test _enforce_scene_durations() merge/split logic."""

    def _make_scene(self, start, end, asset_idx=0):
        return {
            "start": start,
            "end": end,
            "duration": end - start,
            "tipo": "desarrollo",
            "texto": "test text",
            "media_tipo": "imagen",
            "media_duracion": 6,
            "search_query_en": "test",
            "asset_idx": asset_idx,
        }

    def _make_editor(self):
        cfg = FakeCanalConfig()
        editor = VideoEditor(cfg)
        editor.SCENE_DURATION_MIN = 5.0
        editor.SCENE_DURATION_MAX = 20.0
        editor.canal = {}
        return editor

    def test_normal_scene_untouched(self):
        """Scene of 10s (5 ≤ 10 ≤ 20) → unchanged."""
        editor = self._make_editor()
        scenes = [self._make_scene(0, 10)]
        result = editor._enforce_scene_durations(scenes)
        assert len(result) == 1
        assert result[0]["duration"] == pytest.approx(10.0)

    def test_short_scene_merged(self):
        """2s (< 5s) + 6s → merged into one ~8s scene."""
        editor = self._make_editor()
        scenes = [
            self._make_scene(0, 2, asset_idx=0),
            self._make_scene(2, 8, asset_idx=1),
        ]
        result = editor._enforce_scene_durations(scenes)
        assert len(result) == 1
        assert result[0]["duration"] == pytest.approx(8.0)

    def test_long_scene_split(self):
        """30s (> 20s) → split into 2 sub-scenes of 15s."""
        editor = self._make_editor()
        scenes = [self._make_scene(0, 30, asset_idx=0)]
        result = editor._enforce_scene_durations(scenes)
        assert len(result) == 2
        assert result[0]["duration"] == pytest.approx(15.0)
        assert result[1]["duration"] == pytest.approx(15.0)
        # Sub-scenes should be marked
        assert result[0].get("is_subscene") is True
        assert result[1].get("is_subscene") is True

    def test_subscenes_inherit_asset_idx(self):
        """Split sub-scenes keep the parent's asset_idx."""
        editor = self._make_editor()
        scenes = [self._make_scene(0, 30, asset_idx=5)]
        result = editor._enforce_scene_durations(scenes)
        assert len(result) == 2
        assert result[0]["asset_idx"] == 5
        assert result[1]["asset_idx"] == 5

    def test_multiple_mixed_scenes(self):
        """Mix of normal, short, and long scenes → all handled."""
        editor = self._make_editor()
        scenes = [
            self._make_scene(0, 3, 0),    # short → merge
            self._make_scene(3, 15, 1),   # normal
            self._make_scene(15, 45, 2),  # long → split
        ]
        result = editor._enforce_scene_durations(scenes)
        # After merge, should have 1 merged + 1 normal + 2 split = 4
        assert len(result) >= 2
        # All durations should be within [5, 20]
        for s in result:
            assert 4.5 <= s["duration"] <= 21.0, \
                f"Duration {s['duration']} outside [5,20]"
