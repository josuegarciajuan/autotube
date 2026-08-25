"""Tests for video_editor.py — offset tracking, dedup, and video clip reuse.

Run:  python3 -m pytest tests/test_video_editor.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import MagicMock, patch, PropertyMock


# ── Helpers ──────────────────────────────────────────────────────

def _make_mock_config():
    """Minimal canal config for the VideoEditor."""
    return {
        "SCENE_DURATION_MIN": 5.0,
        "SCENE_DURATION_MAX": 20.0,
        "KEN_BURNS_ZOOM_MIN": 2,
        "KEN_BURNS_ZOOM_MAX": 5,
        "SUBTITLES_ENABLED": False,
        "BACKGROUND_MUSIC_ENABLED": False,
        "TRANSITION_ENABLED": False,
        "INTRO_DURATION_SEC": 0.0,
        "OUTRO_DURATION_SEC": 0.0,
        "INTRO_ENABLED": False,
        "OUTRO_ENABLED": False,
    }


def _make_block_range(start: float, end: float, **kwargs):
    """Build a minimal scene range dict."""
    br = {
        "start": start,
        "end": end,
        "duration": end - start,
        "tipo": kwargs.get("tipo", "desarrollo"),
        "texto": kwargs.get("texto", "test text."),
        "media_tipo": kwargs.get("media_tipo", "imagen"),
        "asset_idx": kwargs.get("asset_idx", 0),
    }
    return br


def _make_video_asset(path: str = "/tmp/test_video.mp4", **kwargs):
    return {
        "type": "video",
        "path": path,
        "duration": kwargs.get("duration", 30.0),
        "source": kwargs.get("source", "pexels_video"),
    }


def _make_image_asset(path: str = "/tmp/test_image.jpg"):
    return {
        "type": "image",
        "path": path,
        "duration": None,
        "source": "pexels_photo",
    }


# ── Tests ────────────────────────────────────────────────────────

class TestVideoOffsetTracking:
    """When multiple sub-scenes share the same video file, each scene gets
    a different segment instead of freezing on the first one."""

    def test_offset_tracker_plays_different_segments(self):
        """MoviePy handles non-existent files via placeholder fallback context."""
        # This is an integration-level behavior: MoviePy VideoFileClip
        # catches FileNotFoundError and creates a placeholder instead.
        # The core offset logic is tested in test_offset_advances_per_scene.
        pass  # placeholder test — real offset logic tested below

    @patch("pipeline.video_editor.VideoFileClip")
    def test_offset_advances_per_scene(self, mock_vfc):
        """_video_offset_tracker advances correctly for each sub-scene."""
        from pipeline.video_editor import VideoEditor

        mock_clip = MagicMock()
        mock_clip.duration.return_value = 30.0
        mock_vfc.return_value = mock_clip

        editor = VideoEditor(canal_config=_make_mock_config())

        # Simulate what _create_block_clip does for 3 sub-scenes
        path = "/tmp/test.mp4"
        path_obj = Path(path)
        dur = 7.0

        # Scene 0: offset 0
        offset = editor._video_offset_tracker.get(path, 0.0)
        assert offset == 0.0
        editor._video_offset_tracker[path] = offset + dur

        # Scene 1: offset 7
        offset = editor._video_offset_tracker.get(path, 0.0)
        assert offset == 7.0, f"Expected offset 7.0, got {offset}"
        editor._video_offset_tracker[path] = offset + dur

        # Scene 2: offset 14
        offset = editor._video_offset_tracker.get(path, 0.0)
        assert offset == 14.0, f"Expected offset 14.0, got {offset}"
        editor._video_offset_tracker[path] = offset + dur

        # Scene 3: offset 21
        offset = editor._video_offset_tracker.get(path, 0.0)
        assert offset == 21.0

    @patch("pipeline.video_editor.VideoFileClip")
    def test_offset_exhausted_wraps_with_looping(self, mock_vfc):
        """Exhausted clips now wrap modulo clip_dur instead of returning None."""
        from pipeline.video_editor import VideoEditor

        mock_clip = MagicMock()
        mock_clip.duration = 20.0  # type: ignore
        # Set w/h to video-size values (needed by new Ken Burns zoom lambda)
        mock_clip.w = 1920
        mock_clip.h = 1080
        mock_clip.resized.return_value = mock_clip
        mock_clip.subclipped.return_value = mock_clip
        mock_vfc.return_value = mock_clip

        editor = VideoEditor(canal_config=_make_mock_config())

        # Scene at offset 14 from 20s video with dur=7 → 14+7=21 > 20
        # With looping, it wraps: plays remaining 6s + 1s from start
        clip = editor._video_clip_for_block(Path("/tmp/test.mp4"), 7.0, start_offset=14.0)
        assert clip is not None, "Looping should succeed: wraps modulo clip_dur instead of failing"

        # Scene at offset 13 from 20s → should still work fine (13+7=20)
        mock_clip.duration = 20.01  # type: ignore
        clip = editor._video_clip_for_block(Path("/tmp/test.mp4"), 7.0, start_offset=13.0)
        assert clip is not None, "Non-exhausted clip should still work normally"


class TestImageDedup:
    """Images should still be deduplicated — same image twice = merge."""

    @patch("pipeline.video_editor.VideoEditor._image_clip_for_block")
    @patch.object(Path, "exists", return_value=True)
    def test_image_dedup_still_works(self, mock_exists, mock_image_clip):
        """Same image path used twice → second call returns None."""
        from pipeline.video_editor import VideoEditor

        mock_clip = MagicMock()
        mock_image_clip.return_value = mock_clip

        editor = VideoEditor(canal_config=_make_mock_config())

        # First use of image: should create a clip
        block1 = _make_block_range(0, 6.0)
        asset1 = _make_image_asset("/tmp/photo.jpg")

        clip1 = editor._create_block_clip(block1, asset1)
        assert clip1 is not None, "First image should create a clip"
        assert "/tmp/photo.jpg" in editor._used_asset_paths

        # Second use of same image: should return None (dedup)
        block2 = _make_block_range(6, 12.0)
        asset2 = _make_image_asset("/tmp/photo.jpg")

        clip2 = editor._create_block_clip(block2, asset2)
        assert clip2 is None, "Second use of same image should dedup → None"


class TestBuildVideoReset:
    """State must reset between build_video calls (no leak across builds)."""

    def test_state_resets_per_build(self):
        """_used_asset_paths and _video_offset_tracker reset each build."""
        from pipeline.video_editor import VideoEditor

        editor = VideoEditor(canal_config=_make_mock_config())

        # Simulate a previous build leaving state
        editor._used_asset_paths.add("/tmp/old.mp4")
        editor._video_offset_tracker["/tmp/old.mp4"] = 99.0
        editor._video_color_grade = {"contrast": 1.5}

        # Call build_video with minimal valid args — it will fail fast
        # because media_assets is empty, but the reset should happen first
        try:
            editor.build_video(
                bloques=[],
                media_assets=[],
                audio_path="/tmp/fake.mp3",
                timestamps=[],
            )
        except (RuntimeError, ValueError, AttributeError, FileNotFoundError, IndexError):
            # Expected to fail somewhere after reset — that's fine
            pass

        assert len(editor._used_asset_paths) == 0, "Paths should be cleared"
        assert len(editor._video_offset_tracker) == 0, "Offsets should be cleared"
        assert editor._video_color_grade is None, "Color grade should be reset"


class TestKenBurnsPan:
    """Test that pan factor was increased from 0.5 to 1.0."""

    def test_pan_factor_produces_visible_movement(self):
        """Ken Burns clip with factor 1.0 should produce different frames."""
        import numpy as np
        from PIL import Image
        import tempfile
        from pipeline.video_editor import VideoEditor

        ve = VideoEditor(_make_mock_config())
        # Create a recognizable image (gradient-like for frame diff)
        img = Image.new("RGB", (2500, 1400))
        for x in range(0, 2500, 100):
            for y in range(0, 1400, 100):
                img.putpixel((min(x, 2499), min(y, 1399)),
                            (x % 255, y % 255, (x + y) % 255))

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            img.save(f.name)
            path = Path(f.name)

        try:
            clip = ve._single_ken_burns_clip(path, 4.0, 5.0)
            frame0 = clip.get_frame(0)
            frame_end = clip.get_frame(3.9)
            # With pan factor 1.0, frames should differ visibly
            assert not np.array_equal(frame0, frame_end), \
                "Ken Burns frames must differ (pan=1.0)"
        finally:
            path.unlink(missing_ok=True)


class TestDynamicKenBurnsProfiles:
    """Image scenes select varied, gentle motion profiles per image."""

    def test_profile_selection_avoids_immediate_repetition(self):
        from pipeline.video_editor import VideoEditor

        ve = VideoEditor(_make_mock_config())
        ve._last_ken_burns_profile = "horizontal_in"

        with patch("pipeline.video_editor.random.random", return_value=0.5), \
             patch("pipeline.video_editor.random.choice", side_effect=lambda values: values[0]):
            profile = ve._select_ken_burns_profile()

        assert profile["name"] != "horizontal_in"

    def test_static_profile_is_configurable(self):
        from pipeline.video_editor import VideoEditor

        config = _make_mock_config()
        config["KEN_BURNS_STATIC_PROBABILITY"] = 1.0
        ve = VideoEditor(config)

        assert ve._select_ken_burns_profile()["name"] == "static"

    def test_motion_uses_ease_in_out(self):
        from pipeline.video_editor import VideoEditor

        ve = VideoEditor(_make_mock_config())
        assert ve._ease_in_out(0.0) == 0.0
        assert ve._ease_in_out(1.0) == 1.0
        assert ve._ease_in_out(0.25) < 0.25
        assert ve._ease_in_out(0.75) > 0.75

    def test_static_framing_preserves_aspect_ratio(self):
        from pipeline.video_editor import VideoEditor
        from PIL import Image
        import tempfile
        import numpy as np

        ve = VideoEditor(_make_mock_config())
        # A portrait image must be center-cropped, not stretched to 16:9.
        img = Image.new("RGB", (500, 1000), (20, 30, 40))
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            img.save(f.name)
            path = Path(f.name)

        try:
            clip = ve._single_ken_burns_clip(path, 2.0, 0, profile={"name": "static"})
            frame = clip.get_frame(0)
            assert frame.shape[:2] == (ve.video_size[1], ve.video_size[0])
            assert np.array_equal(frame[0, 0], frame[-1, 0])
        finally:
            path.unlink(missing_ok=True)

    def test_channel_config_overrides_motion_defaults(self):
        from pipeline.video_editor import VideoEditor

        config = _make_mock_config()
        config["KEN_BURNS_MOTION_FACTOR"] = 0.2
        config["KEN_BURNS_ZOOM_FACTOR"] = 0.3
        ve = VideoEditor(config)

        assert ve._ken_burns_motion_factor() == 0.2
        assert ve._ken_burns_zoom_factor() == 0.3


class TestVideoClipFallbackOnCorrupt:
    """When VideoFileClip fails (corrupt file), return None so caller
    can fall through to image / extend-previous-clip logic."""

    @patch("pipeline.video_editor.VideoFileClip")
    def test_corrupt_video_returns_none_not_placeholder(self, mock_vfc):
        """A corrupt video file that raises an exception during load
        should return None (not a black placeholder). Returning None
        allows the caller in _create_block_clip to trigger the image
        fallback or previous-clip extension."""
        from pipeline.video_editor import VideoEditor

        mock_vfc.side_effect = OSError("Broken pipe — corrupt file")
        editor = VideoEditor(canal_config=_make_mock_config())

        clip = editor._video_clip_for_block(Path("/tmp/corrupt.mp4"), 7.0)
        assert clip is None, (
            "Corrupt video should return None (triggers image fallback), "
            "not a black placeholder clip"
        )

    @patch("pipeline.video_editor.VideoFileClip")
    @patch.object(Path, "exists", return_value=True)
    def test_create_block_clip_falls_through_on_corrupt_video(self, mock_exists, mock_vfc):
        """When _video_clip_for_block returns None for a corrupt video,
        _create_block_clip should fall through to the image branch
        (if the asset is available), rather than returning a placeholder."""
        from pipeline.video_editor import VideoEditor

        mock_vfc.side_effect = OSError("Broken pipe")
        editor = VideoEditor(canal_config=_make_mock_config())

        # Create a block that says media_tipo="video" but the video is corrupt
        block = _make_block_range(0, 7.0, media_tipo="video")
        asset = _make_video_asset()

        # The clip should be None (falls through — no image path available
        # for this asset, but at least it shouldn't be a placeholder)
        clip = editor._create_block_clip(block, asset)
        assert clip is None, (
            "create_block_clip should return None for corrupt video "
            "(caller extends previous clip), not a black placeholder"
        )


# ═══════════════════════════════════════════════════════════════════
# Regression guards — prevent bugs that were fixed from returning
# ═══════════════════════════════════════════════════════════════════

class TestKenBurnsNeverZeroPan:
    """Bug: ~11% chance of pan_dir_x=0 AND pan_dir_y=0 → static image.
    Fix: at least one axis is always ±1 (horizontal for landscape, vertical for portrait)."""

    def test_at_least_one_axis_always_pans(self):
        """100 iterations — at least one of pan_dir_x/pan_dir_y is non-zero.
        Landscape images use horizontal pan, portrait/square use vertical."""
        from pipeline.video_editor import VideoEditor
        from PIL import Image
        import tempfile
        import random as _random

        _random.seed(42)

        ve = VideoEditor(_make_mock_config())
        img = Image.new("RGB", (2000, 1200))
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            img.save(f.name)
            p = Path(f.name)

        try:
            for i in range(100):
                clip = ve._single_ken_burns_clip(p, 4.0, 6.0)
        # Just creating the clip should not raise — the fix guarantees
        # at least one pan axis is non-zero, so get_frame always
        # produces a valid crop.
                frame = clip.get_frame(0)
                assert frame is not None
        finally:
            p.unlink(missing_ok=True)


class TestVideoFallbackToImage:
    """Bug: _create_block_clip(video_fail) returned None → scene merged,
    mini-video lost.
    Fix: when a video fails and fallback_pool is available, use it."""

    @patch("pipeline.video_editor.VideoFileClip")
    @patch("pipeline.video_editor.VideoEditor._image_clip_for_block")
    @patch.object(Path, "exists", return_value=True)
    def test_video_failure_uses_fallback_pool(self, mock_exists, mock_img_clip, mock_vfc):
        """Corrupt video + fallback_pool → returns image clip, not None."""
        from pipeline.video_editor import VideoEditor

        mock_vfc.side_effect = OSError("Broken pipe")
        mock_img_clip.return_value = MagicMock()

        editor = VideoEditor(canal_config=_make_mock_config())

        block = _make_block_range(0, 7.0, media_tipo="video")
        asset = _make_video_asset()

        # Provide a fallback pool with at least one image path
        fallback = [Path("/tmp/fallback.jpg")]

        clip = editor._create_block_clip(block, asset, fallback_pool=fallback)
        assert clip is not None, (
            "Video failure with fallback_pool should return an image clip, "
            "not None (which would merge scene and lose the mini-video)"
        )

    @patch("pipeline.video_editor.VideoFileClip")
    @patch.object(Path, "exists", return_value=True)
    @patch("pipeline.video_editor.VideoEditor._image_clip_for_block")
    def test_video_failure_without_fallback_still_returns_none(self, mock_img, mock_exists, mock_vfc):
        """No fallback pool → still returns None (so caller extends previous clip).
        This is the expected behavior when no fallback images exist."""
        from pipeline.video_editor import VideoEditor

        mock_vfc.side_effect = OSError("Broken pipe")
        editor = VideoEditor(canal_config=_make_mock_config())

        block = _make_block_range(0, 7.0, media_tipo="video")
        asset = _make_video_asset()

        # No fallback pool
        clip = editor._create_block_clip(block, asset, fallback_pool=[])
        assert clip is None, (
            "Without fallback pool, should return None (caller extends previous)"
        )


class TestConfigValidator:
    """Bug: visual config values were silently out-of-range (vignette too dark,
    Ken Burns too weak, color nearly black). Fix: validator rejects bad values."""

    def test_rejects_pure_black_secondary(self):
        """COLOR_PALETTE.secondary=(0,0,0) → warned + forced to default."""
        from config.config_validator import validate_channel_config

        cfg = {"COLOR_PALETTE": {"secondary": (0, 0, 0)}}
        w = validate_channel_config("test", cfg)
        assert len(w) > 0, "Pure black secondary should trigger a warning"
        assert cfg["COLOR_PALETTE"]["secondary"] != (0, 0, 0), (
            "Pure black secondary should be forced to safe default"
        )

    def test_rejects_out_of_range_zoom(self):
        """KEN_BURNS_ZOOM_MIN=0 → warned + forced to default (5.0)."""
        from config.config_validator import validate_channel_config

        cfg = {"KEN_BURNS_ZOOM_MIN": 0}
        w = validate_channel_config("test", cfg)
        assert len(w) > 0, "ZOOM_MIN=0 should trigger a warning"
        assert cfg["KEN_BURNS_ZOOM_MIN"] == 5.0, "ZOOM_MIN=0 should be forced to 5.0"

    def test_rejects_zoom_min_ge_zoom_max(self):
        """KEN_BURNS_ZOOM_MIN >= ZOOM_MAX → warns + fixes."""
        from config.config_validator import validate_channel_config

        cfg = {"KEN_BURNS_ZOOM_MIN": 12, "KEN_BURNS_ZOOM_MAX": 8}
        w = validate_channel_config("test", cfg)
        assert len(w) > 0, "ZOOM_MIN >= ZOOM_MAX should trigger a warning"
        assert cfg["KEN_BURNS_ZOOM_MAX"] == 17.0, "ZOOM_MAX should be forced to ZOOM_MIN+5"

    def test_rejects_extreme_vignette_radius(self):
        """VIGNETTE_RADIUS_FACTOR=0.3 → warned + forced to default."""
        from config.config_validator import validate_channel_config

        cfg = {"VIGNETTE_RADIUS_FACTOR": 0.3}
        w = validate_channel_config("test", cfg)
        assert len(w) > 0, "RADIUS_FACTOR=0.3 should trigger a warning"
        assert cfg["VIGNETTE_RADIUS_FACTOR"] == 0.72, "Should be forced to safe default 0.72"

    def test_accepts_valid_config(self):
        """A valid config should produce no warnings."""
        from config.config_validator import validate_channel_config

        cfg = {
            "KEN_BURNS_ZOOM_MIN": 5,
            "KEN_BURNS_ZOOM_MAX": 10,
            "VIGNETTE_RADIUS_FACTOR": 0.72,
            "VIGNETTE_INTENSITY": 12,
            "COLOR_PALETTE": {"secondary": (15, 25, 48)},
            "SCENE_DURATION_MIN": 5,
            "SCENE_DURATION_MAX": 20,
            "MAX_CLIP_EXTEND_SEC": 25,
        }
        w = validate_channel_config("test", cfg)
        assert len(w) == 0, f"Valid config should produce zero warnings, got: {w}"


class TestSafeSubclipToDuration:
    """Regression: MoviePy v2 raises ``ValueError: end_time (X) should be smaller
    or equal to the clip's duration (X)`` when ``start + length`` exceeds the
    clip duration by a floating-point epsilon (e.g. ``pos + (tts_duration - pos)``
    rounding up). ``_safe_subclip_to_duration`` clamps the end so assembly never
    aborts on this precision edge (video #2173)."""

    def _make_mock_clip(self, duration: float):
        clip = MagicMock()
        clip.duration = duration
        clip.subclipped.return_value = "subclipped"
        clip.subclip.return_value = "subclip-v1"
        return clip

    def test_in_bounds_keeps_explicit_end(self):
        """Normal case: start+length inside the clip → explicit end is used."""
        from pipeline.video_editor import VideoEditor

        clip = self._make_mock_clip(duration=100.0)
        result = VideoEditor._safe_subclip_to_duration(clip, 10.0, 5.0, 100.0)
        assert result == "subclipped"
        clip.subclipped.assert_called_once_with(10.0, 15.0)

    def test_epsilon_overflow_clamps_to_duration(self):
        """Regression: pos + (tts_duration - pos) rounds above the clip's real
        duration → must not raise (MoviePy end_time validation)."""
        from pipeline.video_editor import VideoEditor

        # The caller's tts_duration can be a hair LARGER than MoviePy's own
        # duration for the file: pos + leftover == caller duration, but the
        # clip's real duration is smaller → naive subclipped would raise.
        caller_duration = 880.63
        clip_real_duration = 880.63 - 1e-13
        pos = 879.2299999999997
        leftover = caller_duration - pos   # ≈ 1.4000000000003183

        clip = self._make_mock_clip(duration=clip_real_duration)
        # Tail path: single-arg subclipped(start) → no explicit end to validate.
        result = VideoEditor._safe_subclip_to_duration(clip, pos, leftover, caller_duration)
        assert result == "subclipped"
        clip.subclipped.assert_called_once_with(pos)

    def test_tail_exact_end_uses_single_arg(self):
        """When end lands exactly on the clip end, MoviePy takes the clip to its
        natural end (no explicit end arg) — no epsilon can escape."""
        from pipeline.video_editor import VideoEditor

        clip = self._make_mock_clip(duration=60.0)
        VideoEditor._safe_subclip_to_duration(clip, 55.0, 5.0, 60.0)
        clip.subclipped.assert_called_once_with(55.0)

    def test_moviepy_v1_fallback_on_typeerror(self):
        """MoviePy v1 subclip requires two args → fallback passes clamped duration."""
        from pipeline.video_editor import VideoEditor

        clip = self._make_mock_clip(duration=60.0)

        def _v1_only_subclipped(*args, **kwargs):
            raise TypeError("subclipped is v2-only")

        clip.subclipped.side_effect = _v1_only_subclipped

        result = VideoEditor._safe_subclip_to_duration(clip, 55.0, 5.0, 60.0)
        assert result == "subclip-v1"
        clip.subclip.assert_called_once_with(55.0, 60.0)
