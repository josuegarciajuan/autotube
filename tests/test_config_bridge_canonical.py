"""Tests for canonical config key normalization and duplicate safety."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config.config_bridge as bridge


def test_canonical_key_maps_lowercase_variant():
    assert bridge.canonical_config_key("title_formulas", "canal2") == "TITLE_FORMULAS"
    assert bridge.canonical_config_key("title_power_words", "canal2") == "TITLE_POWER_WORDS"
    assert bridge.canonical_config_key("prod_video_duration_max", "canal2") == "PROD_VIDEO_DURATION_MAX"


def test_canonical_key_keeps_unknown_names():
    assert bridge.canonical_config_key("NOT_A_REAL_KEY", "canal2") == "NOT_A_REAL_KEY"


def test_normalize_patch_uses_canonical_spelling():
    out = bridge.normalize_config_patch(
        {"title_formulas": ["A"], "seo_primary_keyword": "x"},
        "canal2",
    )
    assert out == {"TITLE_FORMULAS": ["A"], "SEO_PRIMARY_KEYWORD": "x"}


def test_normalize_patch_canonical_wins_over_duplicate():
    out = bridge.normalize_config_patch(
        {"title_formulas": ["old"], "TITLE_FORMULAS": ["new"]},
        "canal2",
    )
    assert out == {"TITLE_FORMULAS": ["new"]}


def test_clean_duplicate_keys_removes_equal_values():
    cleaned = bridge._clean_duplicate_keys(
        {"TITLE_FORMULAS": ["same"], "title_formulas": ["same"]},
        {"TITLE_FORMULAS": ["same"]},
    )
    assert cleaned == {"TITLE_FORMULAS": ["same"]}


def test_clean_duplicate_keys_keeps_conflicting_values():
    raw = {"TITLE_FORMULAS": ["canonical"], "title_formulas": ["insight-applied"]}
    cleaned = bridge._clean_duplicate_keys(raw, {"TITLE_FORMULAS": ["canonical"]})
    # Both survive: silently deleting one would discard an accepted change.
    assert cleaned == raw
