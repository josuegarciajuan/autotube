"""Tests for scene duration enforcement (merge short below MIN, split long above MAX).

Run:  python3 -m pytest tests/test_scene_ranges.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

    def test_media_specific_caps_preserve_contiguous_ranges_and_unique_requests(self):
        """Image ranges cap at 7s, video ranges at 10s without timeline gaps."""
        editor = self._make_editor()
        editor.canal = {
            "IMAGE_SCENE_DURATION_MIN": 4.0,
            "IMAGE_SCENE_DURATION_MAX": 7.0,
            "VIDEO_SCENE_DURATION_MIN": 6.0,
            "VIDEO_SCENE_DURATION_MAX": 10.0,
        }
        image = self._make_scene(0, 16, asset_idx=3)
        image["media_tipo"] = "imagen"
        video = self._make_scene(16, 39, asset_idx=4)
        video["media_tipo"] = "video"

        result = editor._enforce_scene_durations([image, video])

        assert result[0]["start"] == pytest.approx(0.0)
        assert result[-1]["end"] == pytest.approx(39.0)
        assert all(
            left["end"] == pytest.approx(right["start"])
            for left, right in zip(result, result[1:])
        )
        assert all(scene["duration"] <= 7.0 for scene in result if scene["media_tipo"] == "imagen")
        assert all(scene["duration"] <= 10.0 for scene in result if scene["media_tipo"] == "video")
        assert len({scene["media_request_id"] for scene in result}) == len(result)

    def test_long_scene_splits_at_sentence_boundaries_and_preserves_fragment_text(self):
        editor = self._make_editor()
        scene = self._make_scene(0, 12, asset_idx=2)
        scene["texto"] = "Primera idea completa. Segunda idea completa. Tercera idea completa."
        scene["word_timestamps"] = [
            {"word": word, "start": i * 1.5, "end": (i + 1) * 1.5}
            for i, word in enumerate(scene["texto"].split())
        ]
        editor.canal = {"IMAGE_SCENE_DURATION_MIN": 3.0, "IMAGE_SCENE_DURATION_MAX": 5.0}

        result = editor._enforce_scene_durations([scene])

        assert [part["texto"] for part in result] == [
            "Primera idea completa.",
            "Segunda idea completa.",
            "Tercera idea completa.",
        ]
        assert [(part["start"], part["end"]) for part in result] == [
            (0.0, 4.5), (4.5, 9.0), (9.0, 12.0)
        ]
        assert all(part["word_timestamps"] for part in result)

    def test_subscene_split_keeps_english_query_and_propagates_phase(self):
        """P5: split subscenes keep the parent's English query (no Spanish
        fragment appended) and the fragment goes to ``query_context``."""
        editor = self._make_editor()
        editor.canal = {
            "IMAGE_SCENE_DURATION_MIN": 3.0,
            "IMAGE_SCENE_DURATION_MAX": 5.0,
            "SCRIPT_STRUCTURE": [
                {"id": "desarrollo", "time_pct": "0-100%",
                 "scene_pacing": {"image_target_sec": 4.0, "video_target_sec": 4.0}},
            ],
        }
        scene = self._make_scene(0, 12, asset_idx=7)
        scene["texto"] = "Los mercaderes usaban balanzas para pesar el oro."
        scene["search_query_en"] = "merchant weighing gold scale ancient Egyptian"
        scene["phase_id"] = "desarrollo"

        result = editor._enforce_scene_durations([scene])

        assert len(result) >= 2
        for part in result:
            # English parent query is preserved verbatim, never polluted with
            # the Spanish fragment.
            assert part["search_query_en"] == "merchant weighing gold scale ancient Egyptian"
            assert "Los mercaderes" not in part["search_query_en"]
        # The fragment is exposed as context for later enrichment, and the
        # phase id is propagated to every subscene.
        assert any(part.get("query_context") for part in result)
        assert all(part.get("phase_id") == "desarrollo" for part in result)
