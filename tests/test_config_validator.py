"""Unit tests for per-channel config validation (scene pacing + structure)."""

import sys
from pathlib import Path

import pytest

# Raíz del repo dinámica (worktree o árbol principal) — igual que conftest.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import defaults
from config.config_validator import validate_channel_config


def _valid_structure():
    """A canonical, contiguous 7-phase structure with in-range pacing."""
    return [
        {"id": "gancho", "step": "G", "time_pct": "0-10%",
         "scene_pacing": {"image_target_sec": 5.0, "video_target_sec": 4.5}},
        {"id": "contexto", "step": "C", "time_pct": "10-20%",
         "scene_pacing": {"image_target_sec": 6.0, "video_target_sec": 5.5}},
        {"id": "protagonistas", "step": "P", "time_pct": "20-30%",
         "scene_pacing": {"image_target_sec": 6.0, "video_target_sec": 5.5}},
        {"id": "desarrollo", "step": "D", "time_pct": "30-55%",
         "scene_pacing": {"image_target_sec": 6.0, "video_target_sec": 6.0}},
        {"id": "climax", "step": "K", "time_pct": "55-70%",
         "scene_pacing": {"image_target_sec": 5.0, "video_target_sec": 4.5}},
        {"id": "consecuencias", "step": "S", "time_pct": "70-85%",
         "scene_pacing": {"image_target_sec": 6.0, "video_target_sec": 5.5}},
        {"id": "cierre", "step": "F", "time_pct": "85-100%",
         "scene_pacing": {"image_target_sec": 7.0, "video_target_sec": 6.0}},
    ]


def test_valid_structure_raises_no_warnings():
    config = {
        "IMAGE_SCENE_DURATION_MIN": 5.0,
        "IMAGE_SCENE_DURATION_MAX": 8.0,
        "VIDEO_SCENE_DURATION_MIN": 4.0,
        "VIDEO_SCENE_DURATION_MAX": 7.0,
        "SCRIPT_STRUCTURE": _valid_structure(),
        "MEDIA_STRATEGY": {"hook_climax_video_requires_bible": True},
    }
    warnings = validate_channel_config("canalX", config)
    assert warnings == []


def test_default_ranges_accept_per_channel_overrides():
    # canal3-style: image 6-9, video 5-8 (targets inside those bounds)
    structure = [
        {"id": f"p{i}", "step": "X", "time_pct": f"{i*10}-{(i+1)*10}%",
         "scene_pacing": {"image_target_sec": 7.0, "video_target_sec": 6.0}}
        for i in range(10)
    ]
    config = {
        "IMAGE_SCENE_DURATION_MIN": 6.0,
        "IMAGE_SCENE_DURATION_MAX": 9.0,
        "VIDEO_SCENE_DURATION_MIN": 5.0,
        "VIDEO_SCENE_DURATION_MAX": 8.0,
        "SCRIPT_STRUCTURE": structure,
    }
    warnings = validate_channel_config("canal3", config)
    assert warnings == []


def test_duplicate_phase_id_warns():
    structure = _valid_structure()
    structure[1]["id"] = "gancho"  # duplicate
    config = {"SCRIPT_STRUCTURE": structure}
    warnings = validate_channel_config("canalX", config)
    assert any("duplicate phase id" in w for w in warnings)


def test_non_contiguous_phases_warn():
    structure = _valid_structure()
    structure[3]["time_pct"] = "35-55%"  # gap 30%→35%
    config = {"SCRIPT_STRUCTURE": structure}
    warnings = validate_channel_config("canalX", config)
    assert any("not contiguous" in w for w in warnings)


def test_out_of_range_pacing_is_clamped():
    structure = _valid_structure()
    structure[0]["scene_pacing"]["image_target_sec"] = 12.0  # > 8
    config = {
        "IMAGE_SCENE_DURATION_MAX": 8.0,
        "SCRIPT_STRUCTURE": structure,
    }
    warnings = validate_channel_config("canalX", config)
    assert any("out of range" in w for w in warnings)
    assert structure[0]["scene_pacing"]["image_target_sec"] == pytest.approx(6.5)


def test_non_boolean_hook_climax_policy_forced():
    config = {"MEDIA_STRATEGY": {"hook_climax_video_requires_bible": "yes"}}
    warnings = validate_channel_config("canalX", config)
    assert any("hook_climax_video_requires_bible" in w for w in warnings)
    assert config["MEDIA_STRATEGY"]["hook_climax_video_requires_bible"] is True


def test_missing_phase_id_warns():
    structure = _valid_structure()
    del structure[0]["id"]
    config = {"SCRIPT_STRUCTURE": structure}
    warnings = validate_channel_config("canalX", config)
    assert any("missing 'id'" in w for w in warnings)


def test_new_defaults_are_hard_soft_aligned():
    # MIN must be < MAX for both media types in the shipped defaults.
    assert defaults.IMAGE_SCENE_DURATION_MIN < defaults.IMAGE_SCENE_DURATION_MAX
    assert defaults.VIDEO_SCENE_DURATION_MIN < defaults.VIDEO_SCENE_DURATION_MAX
    assert defaults.IMAGE_SCENE_DEFAULT_TARGET >= defaults.IMAGE_SCENE_DURATION_MIN
    assert defaults.IMAGE_SCENE_DEFAULT_TARGET <= defaults.IMAGE_SCENE_DURATION_MAX
    assert defaults.VIDEO_SCENE_DEFAULT_TARGET >= defaults.VIDEO_SCENE_DURATION_MIN
    assert defaults.VIDEO_SCENE_DEFAULT_TARGET <= defaults.VIDEO_SCENE_DURATION_MAX
    assert defaults.MEDIA_STRATEGY.get("hook_climax_video_requires_bible") is True
