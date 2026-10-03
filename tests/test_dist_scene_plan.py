"""Tests del planificador de escenas distribuidas (invariantes de dedup y frames)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pipeline_dist.scene_plan import (  # noqa: E402
    DuplicateAssetError,
    MissingAssetError,
    build_manifests,
)


def _scene(dur):
    return {"start": 0.0, "end": dur, "duration": dur}


def _asset(kind, path, chash=""):
    return {"type": kind, "path": path, "content_hash": chash}


def test_manifests_frames_exactos_y_1a1():
    ranges = [_scene(2.0), _scene(1.5)]
    assets = [_asset("image", "/tmp/a.jpg", "h1"), _asset("video", "/tmp/b.mp4", "h2")]
    mans = build_manifests(ranges, assets, fps=30, width=1920, height=1080, seed=7)
    assert [m["frames"] for m in mans] == [60, 45]
    assert mans[0]["scene_key"] == "0000"
    assert mans[1]["scene_key"] == "0001"
    # duración derivada de frames (sin deriva por redondeo)
    assert mans[1]["duration"] == pytest.approx(1.5)


def test_manifests_rechaza_asset_repetido():
    ranges = [_scene(1.0), _scene(1.0)]
    assets = [_asset("image", "/tmp/same.jpg", "h"), _asset("image", "/tmp/same.jpg", "h")]
    with pytest.raises(DuplicateAssetError):
        build_manifests(ranges, assets)


def test_manifests_rechaza_placeholder():
    ranges = [_scene(1.0), _scene(1.0)]
    assets = [_asset("image", "/tmp/a.jpg", "h1"), {"type": "placeholder", "path": None}]
    with pytest.raises(MissingAssetError):
        build_manifests(ranges, assets)


def test_manifests_rechaza_no_1a1():
    with pytest.raises(Exception):
        build_manifests([_scene(1.0)], [])
