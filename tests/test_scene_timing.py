"""Unit tests for exact scene timing and body sync gates."""

import sys
from pathlib import Path

import pytest

# Raíz del repo dinámica (worktree o árbol principal) — igual que conftest.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import defaults
from config.config_validator import validate_channel_config
from pipeline.media_fetcher import MediaFetcher
from pipeline.video_editor import VideoEditor


def test_scene_timing_defaults_and_cross_parameter_validation():
    assert defaults.IMAGE_SCENE_DURATION_MIN == 5.0
    assert defaults.IMAGE_SCENE_DURATION_MAX == 7.0
    assert defaults.VIDEO_SCENE_DURATION_MIN == 4.0
    assert defaults.VIDEO_SCENE_DURATION_MAX == 6.0
    assert defaults.IMAGE_SCENE_DEFAULT_TARGET == 5.5
    assert defaults.VIDEO_SCENE_DEFAULT_TARGET == 5.0
    assert defaults.SCENE_SYNC_TOLERANCE_SEC == 0.15
    assert not hasattr(defaults, "SCENE_TRANSITION_DURATION_SEC")

    config = {
        "IMAGE_SCENE_DURATION_MIN": 8.0,
        "IMAGE_SCENE_DURATION_MAX": 7.0,
        "VIDEO_SCENE_DURATION_MIN": 10.0,
        "VIDEO_SCENE_DURATION_MAX": 9.0,
    }
    warnings = validate_channel_config("test", config)

    assert any("IMAGE_SCENE_DURATION_MIN" in warning for warning in warnings)
    assert any("VIDEO_SCENE_DURATION_MIN" in warning for warning in warnings)
    assert config["IMAGE_SCENE_DURATION_MAX"] > config["IMAGE_SCENE_DURATION_MIN"]
    assert config["VIDEO_SCENE_DURATION_MAX"] > config["VIDEO_SCENE_DURATION_MIN"]


def test_concat_filter_has_exact_duration_without_overlap_or_padding():
    filter_complex, output_label = VideoEditor._build_duration_preserving_concat_filter(
        3, film_grain_opacity=0, vignette_intensity=0,
    )

    assert "concat=n=3:v=1:a=0" in filter_complex
    assert "xfade" not in filter_complex
    assert "tpad" not in filter_complex
    assert output_label == "[concat]"


def test_concat_filter_pads_each_segment_to_its_planned_duration():
    durations = [6.667, 12.542, 8.875]
    filter_complex, output_label = VideoEditor._build_duration_preserving_concat_filter(
        3, film_grain_opacity=0, vignette_intensity=0,
        planned_durations=durations, fps=24,
    )

    assert "concat=n=3:v=1:a=0" in filter_complex
    assert "xfade" not in filter_complex
    # One clip-path chain per input, each pad-then-trimmed to an exact length.
    assert filter_complex.count("tpad=stop_mode=clone") == 3
    assert filter_complex.count("setpts=PTS-STARTPTS") == 3
    assert "[k0]" in filter_complex and "[k2]" in filter_complex
    # Frame-quantised cumulative boundaries: the sum telescopes to the planned
    # total within a single frame, instead of one dropped frame per scene.
    import re
    ends = [float(m) for m in re.findall(r"trim=start=0:end=([0-9.]+)", filter_complex)]
    assert len(ends) == 3
    assert sum(ends) == pytest.approx(sum(durations), abs=1.0 / 24.0)
    assert output_label == "[concat]"


def test_concat_filter_rejects_duration_count_mismatch():
    with pytest.raises(ValueError, match="planned_durations"):
        VideoEditor._build_duration_preserving_concat_filter(
            2, film_grain_opacity=0, vignette_intensity=0,
            planned_durations=[1.0], fps=24,
        )


def test_body_sync_gate_allows_tolerance_and_rejects_larger_mismatch():
    editor = VideoEditor({"SCENE_SYNC_TOLERANCE_SEC": 0.15,
                          "LONGFORM_MAX_TRAILING_AUDIO_SEC": 2.0})

    # Trailing TTS silence (audio slightly longer than video) is benign.
    editor._assert_body_timeline_sync(10.0, 10.14)
    editor._assert_body_timeline_sync(10.0, 10.5)

    # Extra video frames are harmless: -shortest trims video, never narration.
    editor._assert_body_timeline_sync(10.3, 10.0)

    # Missing narration beyond the trailing window is a real bug → fail.
    with pytest.raises(RuntimeError, match="Body narration is shorter"):
        editor._assert_body_timeline_sync(42.5, 40.0)


def test_body_sync_gate_diagnoses_planned_timeline_deficit():
    editor = VideoEditor({"SCENE_SYNC_TOLERANCE_SEC": 0.15,
                          "LONGFORM_MAX_TRAILING_AUDIO_SEC": 2.0})

    # The nov-2026 bug: the rendered body was shorter than the *plan*, while the
    # audio still matched the plan. The message must point at frame rounding /
    # segment loss, not at a planning gap.
    with pytest.raises(RuntimeError, match="Rendered body is shorter than the planned timeline"):
        editor._assert_body_timeline_sync(10.0, 12.5, planned_duration=12.5)

    # TTS dropped a block (audio shorter than the plan).
    with pytest.raises(RuntimeError, match="Narration is shorter than the planned timeline"):
        editor._assert_body_timeline_sync(12.5, 9.0, planned_duration=12.5)

    # Within the trailing-silence tolerance → accepted.
    editor._assert_body_timeline_sync(12.4, 12.5, planned_duration=12.5)


def test_concat_filter_110_scenes_does_not_accumulate_frame_rounding():
    """Regression: 110 scenes must lose at most one frame in total, not one
    frame per scene (the nov-2026 2.2 s body deficit)."""
    import re
    durations = [7.0 + (i % 17) * 0.123 for i in range(110)]
    filter_complex, _ = VideoEditor._build_duration_preserving_concat_filter(
        110, film_grain_opacity=0, vignette_intensity=0,
        planned_durations=durations, fps=24,
    )
    ends = [float(m) for m in re.findall(r"trim=start=0:end=([0-9.]+)", filter_complex)]
    assert len(ends) == 110
    assert sum(ends) == pytest.approx(sum(durations), abs=1.0 / 24.0)


def test_actual_image_fallback_is_resplit_and_gets_distinct_media_requests():
    """A 10s video plan that returns an image becomes two unique 5s image scenes."""
    fetcher = object.__new__(MediaFetcher)
    fetcher._config = {"IMAGE_SCENE_DURATION_MAX": 7.0}
    original_scene = {
        "start": 12.0,
        "end": 22.0,
        "duration": 10.0,
        "media_tipo": "video",
        "media_request_id": "7:0",
        "search_query_en": "ruins documentary",
    }
    original_asset = {"type": "image", "path": "/tmp/first.jpg", "url": "first"}

    ranges, assets = fetcher._reconcile_actual_image_fallbacks(
        [original_scene], [original_asset],
        fetch_distinct_image=lambda scene: {
            "type": "image", "path": "/tmp/second.jpg", "url": "second",
        },
    )

    assert [(scene["start"], scene["end"]) for scene in ranges] == [(12.0, 17.0), (17.0, 22.0)]
    assert all(scene["duration"] <= 7.0 for scene in ranges)
    assert [asset["path"] for asset in assets] == ["/tmp/first.jpg", "/tmp/second.jpg"]
    assert len({scene["media_request_id"] for scene in ranges}) == 2


def test_actual_image_fallback_rejects_a_duplicate_replacement():
    fetcher = object.__new__(MediaFetcher)
    fetcher._config = {"IMAGE_SCENE_DURATION_MAX": 7.0}
    scene = {"start": 0.0, "end": 10.0, "duration": 10.0, "media_request_id": "0:0"}
    original = {"type": "image", "path": "/tmp/first.jpg", "url": "first"}

    _, assets = fetcher._reconcile_actual_image_fallbacks(
        [scene], [original],
        fetch_distinct_image=lambda _: {"type": "image", "path": "/tmp/first.jpg", "url": "other-cdn-url"},
    )

    assert assets[1]["type"] == "placeholder"


def test_actual_image_fallback_does_not_split_into_flashes_below_hard_min():
    """A 6.01s actual image (within soft max 7) stays ONE scene — the old
    code split it into two ~3s flashes (bug)."""
    fetcher = object.__new__(MediaFetcher)
    fetcher._config = {"IMAGE_SCENE_DURATION_MIN": 5.0, "IMAGE_SCENE_DURATION_MAX": 7.0}
    scene = {"start": 12.0, "end": 18.01, "duration": 6.01, "media_tipo": "video"}
    asset = {"type": "image", "path": "/tmp/a.jpg", "url": "a"}

    ranges, assets = fetcher._reconcile_actual_image_fallbacks(
        [scene], [asset], fetch_distinct_image=lambda s: {"type": "image", "path": "/tmp/b.jpg"},
    )
    assert len(ranges) == 1
    assert ranges[0]["duration"] == pytest.approx(6.01)
    assert assets[0]["path"] == "/tmp/a.jpg"


def test_actual_image_fallback_soft_exception_keeps_single_scene():
    """7.5s actual image: no split respects hard min 5 → stays single."""
    fetcher = object.__new__(MediaFetcher)
    fetcher._config = {"IMAGE_SCENE_DURATION_MIN": 5.0, "IMAGE_SCENE_DURATION_MAX": 7.0}
    scene = {"start": 0.0, "end": 7.5, "duration": 7.5, "media_tipo": "video"}
    asset = {"type": "image", "path": "/tmp/a.jpg", "url": "a"}

    ranges, _ = fetcher._reconcile_actual_image_fallbacks(
        [scene], [asset], fetch_distinct_image=lambda s: {"type": "image", "path": "/tmp/b.jpg"},
    )
    assert len(ranges) == 1
    assert ranges[0]["duration"] == pytest.approx(7.5)
    assert ranges[0].get("soft_exception") is True


def test_actual_image_fallback_12s_splits_into_two_6s():
    """12s actual image splits into 6+6 (respects hard min 5 and soft max 7)."""
    fetcher = object.__new__(MediaFetcher)
    fetcher._config = {"IMAGE_SCENE_DURATION_MIN": 5.0, "IMAGE_SCENE_DURATION_MAX": 7.0}
    scene = {"start": 0.0, "end": 12.0, "duration": 12.0, "media_tipo": "video"}
    asset = {"type": "image", "path": "/tmp/a.jpg", "url": "a"}

    ranges, assets = fetcher._reconcile_actual_image_fallbacks(
        [scene], [asset],
        fetch_distinct_image=lambda s: {"type": "image", "path": "/tmp/b.jpg"},
    )
    assert [(r["start"], r["end"]) for r in ranges] == [(0.0, 6.0), (6.0, 12.0)]
    assert assets[0]["path"] == "/tmp/a.jpg"
    assert assets[1]["path"] == "/tmp/b.jpg"


def test_local_sd_defaults_to_native_widescreen_resolution():
    from pipeline.providers.local_sd_provider import LocalSDProvider

    assert (LocalSDProvider.DEFAULT_WIDTH, LocalSDProvider.DEFAULT_HEIGHT) == (768, 432)
