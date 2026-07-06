"""Regression tests for video offset tracking and exhaustion behavior.

Run:  python3 -m pytest tests/test_video_offset_regression.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock


class TestVideoOffsetReset:
    """Video offset tracker MUST reset when a clip is exhausted."""

    def test_offset_resets_on_exhaustion(self):
        """When a video is exhausted, the offset resets to 0 for the next scene."""
        from pipeline.video_editor import VideoEditor

        editor = VideoEditor()
        editor._video_offset_tracker = {}
        editor._used_asset_paths = set()
        editor._image_last_clip_idx = {}
        editor._current_clip_idx = 0
        editor._image_reuse_count = {}

        fake_path = "/tmp/test_video_A.mp4"
        editor._video_offset_tracker[fake_path] = 7.0  # offset advanced from scene 1

        # Mock both Path.exists() and _video_clip_for_block
        with patch.object(Path, "exists", return_value=True), \
             patch.object(editor, "_video_clip_for_block", return_value=None):
            result = editor._create_block_clip(
                block_range={"start": 7.0, "end": 14.0, "duration": 7.0, "tipo": "hook"},
                asset={"type": "video", "path": fake_path, "content_hash": ""},
                clip_idx=1,
            )

        # Should return None (caller will merge)
        assert result is None

        # CRITICAL: offset MUST be reset to 0 after exhaustion
        assert editor._video_offset_tracker[fake_path] == 0.0, (
            f"Offset MUST reset to 0 after exhaustion, got {editor._video_offset_tracker[fake_path]}"
        )

    def test_offset_not_reset_on_success(self):
        """A successful video clip advances the offset (not reset)."""
        from pipeline.video_editor import VideoEditor

        editor = VideoEditor()
        editor._video_offset_tracker = {}
        editor._used_asset_paths = set()
        editor._image_last_clip_idx = {}
        editor._current_clip_idx = 0
        editor._image_reuse_count = {}
        editor._video_color_grade = None

        fake_path = "/tmp/test_video_B.mp4"
        mock_clip = MagicMock()

        with patch.object(Path, "exists", return_value=True), \
             patch.object(editor, "_video_clip_for_block", return_value=mock_clip):
            result = editor._create_block_clip(
                block_range={"start": 0.0, "end": 7.0, "duration": 7.0, "tipo": "hook"},
                asset={"type": "video", "path": fake_path, "content_hash": ""},
                clip_idx=0,
            )

        # Should succeed
        assert result is not None

        # Offset MUST advance (not reset)
        assert editor._video_offset_tracker[fake_path] == 7.0, (
            f"Offset MUST advance to 7.0 after successful clip, got {editor._video_offset_tracker[fake_path]}"
        )

    def test_video_looping_wraps_offset(self):
        """Looping in _video_clip_for_block wraps offset modulo clip duration."""
        from pipeline.video_editor import VideoEditor

        editor = VideoEditor()
        editor.video_size = (480, 270)

        fake_clip = MagicMock()
        fake_clip.duration = 10.0  # 10-second clip
        fake_clip.w = 1920
        fake_clip.h = 1080

        with patch("pipeline.video_editor.VideoFileClip", return_value=fake_clip):
            result = editor._video_clip_for_block(
                Path("/tmp/test.mp4"), block_dur=7.0, start_offset=7.0
            )

        # The looping logic should NOT return None (it uses modulo)
        assert result is not None, "Looping MUST succeed instead of returning None"

    def test_consecutive_exhaustion_resets_each_time(self):
        """Multiple consecutive exhaustions all reset offset to 0."""
        from pipeline.video_editor import VideoEditor

        editor = VideoEditor()
        editor._video_offset_tracker = {}
        editor._used_asset_paths = set()
        editor._image_last_clip_idx = {}
        editor._current_clip_idx = 0
        editor._image_reuse_count = {}

        fake_path = "/tmp/test_video_C.mp4"

        with patch.object(Path, "exists", return_value=True), \
             patch.object(editor, "_video_clip_for_block", return_value=None):
            # Scene 1 exhausts
            editor._create_block_clip(
                block_range={"start": 0, "end": 7, "duration": 7, "tipo": "hook"},
                asset={"type": "video", "path": fake_path, "content_hash": ""},
                clip_idx=0,
            )

        assert editor._video_offset_tracker[fake_path] == 0.0

        with patch.object(Path, "exists", return_value=True), \
             patch.object(editor, "_video_clip_for_block", return_value=None):
            # Scene 2 also exhausts
            editor._create_block_clip(
                block_range={"start": 7, "end": 14, "duration": 7, "tipo": "hook"},
                asset={"type": "video", "path": fake_path, "content_hash": ""},
                clip_idx=1,
            )

        # Still 0 — no zombie offset
        assert editor._video_offset_tracker[fake_path] == 0.0, \
            "Offset must remain 0 after consecutive exhaustions"
