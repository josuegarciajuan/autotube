"""Dedup must not hide recommendations that propose different values."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.services.channel_analyzer import _dedup_recommendations


def _rec(rid, changes, title="Ajustar titulos"):
    return {
        "id": rid,
        "title": title,
        "category": "keywords",
        "config_changes": changes,
    }


def test_same_keys_different_values_are_flagged_not_hidden():
    new = [_rec("new", {"TITLE_FORMULAS": ["nueva formula"]})]
    prev = [_rec("old", {"TITLE_FORMULAS": ["formula anterior"]})]
    out, report = _dedup_recommendations(new, prev, [])
    assert out[0].get("hidden_as_duplicate") is not True
    assert out[0]["conflict_with"] == "old"
    assert out[0]["conflict_keys"] == ["TITLE_FORMULAS"]


def test_same_keys_same_values_are_hidden():
    new = [_rec("new", {"TITLE_FORMULAS": ["igual"]})]
    prev = [_rec("old", {"TITLE_FORMULAS": ["igual"]})]
    out, _ = _dedup_recommendations(new, prev, [])
    assert out[0]["hidden_as_duplicate"] is True
    assert out[0]["duplicate_of"] == "old"
