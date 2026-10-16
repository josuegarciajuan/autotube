"""Tests for pipeline.thumbnail_compare — perceptual local-vs-served check.

These cover the anti "thumbnails.set() returns 200 but YouTube keeps the auto
frame" guard used by the uploader and the thumbnail verification service.
"""

import pytest

from PIL import Image

from pipeline.thumbnail_compare import (
    GRID,
    DEFAULT_THRESHOLD,
    fetch_served_thumbnail,
    thumbnails_match,
)


def _solid(tmp_path, name, color):
    p = tmp_path / name
    Image.new("RGB", (1280, 720), color).save(p, "JPEG", quality=95)
    return p


def test_match_identical_image(tmp_path):
    local = _solid(tmp_path, "local.jpg", (120, 40, 20))
    served = Image.open(local)
    assert thumbnails_match(local, "vid", served_image=served) is True


def test_mismatch_different_scenes(tmp_path):
    local = _solid(tmp_path, "local.jpg", (0, 0, 0))
    other = _solid(tmp_path, "other.jpg", (255, 255, 255))
    served = Image.open(other)
    assert thumbnails_match(local, "vid", served_image=served) is False


def test_threshold_boundary(tmp_path):
    # diff of 10 gray levels > default threshold (8) → mismatch
    local = _solid(tmp_path, "local.jpg", (100, 100, 100))
    other = _solid(tmp_path, "other.jpg", (110, 110, 110))
    served = Image.open(other)
    assert thumbnails_match(local, "vid", threshold=DEFAULT_THRESHOLD,
                            served_image=served) is False
    assert thumbnails_match(local, "vid", threshold=15.0,
                            served_image=served) is True


def test_none_when_local_missing(tmp_path):
    served = Image.new("RGB", (1280, 720), (10, 10, 10))
    assert thumbnails_match(tmp_path / "nope.jpg", "vid", served_image=served) is None


def test_none_when_served_unavailable(tmp_path):
    local = _solid(tmp_path, "local.jpg", (10, 10, 10))
    # served_image=None and network fetch disabled via bogus id + timeout=0
    # Force fetch failure by monkeypatching the fetch helper.
    import pipeline.thumbnail_compare as tc
    orig = tc.fetch_served_thumbnail
    tc.fetch_served_thumbnail = lambda *a, **k: None
    try:
        assert thumbnails_match(local, "vid") is None
    finally:
        tc.fetch_served_thumbnail = orig


def test_grid_constant_sanity():
    assert GRID == 64
    assert DEFAULT_THRESHOLD > 0
