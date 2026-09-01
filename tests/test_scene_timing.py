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


def test_body_sync_gate_allows_tolerance_and_rejects_larger_mismatch():
    editor = VideoEditor({"SCENE_SYNC_TOLERANCE_SEC": 0.15})

    # Trailing TTS silence (audio slightly longer than video) is benign.
    editor._assert_body_timeline_sync(10.0, 10.14)
    editor._assert_body_timeline_sync(10.0, 10.5)

    # Missing narration (audio ends before the body) is a real bug → fail.
    with pytest.raises(RuntimeError, match="Body narration is shorter"):
        editor._assert_body_timeline_sync(10.3, 10.0)


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
