"""Tests for the single-source duration policy."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.duration_policy import (
    apply_range_to_config,
    range_to_mean_disc,
    resolve_duration_range,
    validate_duration_config,
    word_targets,
)


def test_range_to_mean_disc():
    assert range_to_mean_disc(15, 19) == (17.0, 2.0)
    assert range_to_mean_disc(19, 15) == (17.0, 2.0)


def test_objective_is_authoritative():
    cfg = SimpleNamespace(VIDEO_AVERAGE_DURATION_MIN=17, VIDEO_DURATION_DISCREPANCY_MIN=2)
    p = resolve_duration_range(cfg)
    assert (p.min_min, p.max_min) == (15.0, 19.0)
    assert p.source == "objective"


def test_legacy_is_fallback_only():
    cfg = SimpleNamespace(PROD_VIDEO_DURATION_MIN=15, PROD_VIDEO_DURATION_MAX=19)
    p = resolve_duration_range(cfg)
    assert (p.min_min, p.max_min) == (15.0, 19.0)
    assert p.source == "legacy"


def test_legacy_mismatch_is_reported():
    cfg = SimpleNamespace(
        VIDEO_AVERAGE_DURATION_MIN=14,
        VIDEO_DURATION_DISCREPANCY_MIN=4,
        PROD_VIDEO_DURATION_MIN=8,
        PROD_VIDEO_DURATION_MAX=12,
    )
    issues = validate_duration_config(cfg)
    assert any("does not match objective" in i for i in issues)


def test_apply_range_aligns_objective_and_legacy():
    cfg = {"VIDEO_AVERAGE_DURATION_MIN": 14, "VIDEO_DURATION_DISCREPANCY_MIN": 4}
    out = apply_range_to_config(cfg, 15, 19)
    assert out["VIDEO_AVERAGE_DURATION_MIN"] == 17.0
    assert out["VIDEO_DURATION_DISCREPANCY_MIN"] == 2.0
    assert out["PROD_VIDEO_DURATION_MIN"] == 15
    assert out["PROD_VIDEO_DURATION_MAX"] == 19


def test_word_targets_are_positive_and_ordered():
    cfg = SimpleNamespace(
        VIDEO_AVERAGE_DURATION_MIN=17,
        VIDEO_DURATION_DISCREPANCY_MIN=2,
        TTS_ENGINE="edgetts",
        VOICE_RATE="-10%",
    )
    out = word_targets(cfg)
    assert out["words_min"] < out["words_target"] < out["words_max"]
    assert out["duration_min"] == 15.0
    assert out["duration_max"] == 19.0
