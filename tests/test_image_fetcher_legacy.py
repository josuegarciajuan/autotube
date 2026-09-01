"""Tests for the legacy ImageFetcher global-context fix (P8)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.image_fetcher import ImageFetcher


class _Theme:
    era_decade = "17th century"
    era = "17th century"
    primary_subject = "wooden sailing ship"
    forbidden_elements = ["modern city"]


def _fetcher():
    f = object.__new__(ImageFetcher)
    f._config = type("C", (), {"IMAGE_STYLE_MODIFIERS": "", "IMAGES_PER_SCENE": 1})()
    return f


def test_scene_to_query_with_theme_uses_global_context():
    f = _fetcher()
    q = f._scene_to_query("expedicion perdida en el hielo", theme_ctx=_Theme())
    # Led by the global subject + era, not the bare 7-word snippet alone.
    assert "wooden sailing ship" in q
    assert "17th century" in q
    assert "cinematic photography" in q


def test_scene_to_query_without_theme_keeps_legacy_behavior():
    f = _fetcher()
    q = f._scene_to_query("[ESCENA: calle oscura bajo lluvia]")
    # Focused snippet retained, style suffix appended.
    assert "calle" in q
    assert "oscura" in q
    assert "cinematic photography" in q


def test_global_visual_context_none_without_theme():
    f = _fetcher()
    assert f._global_visual_context(None) is None
