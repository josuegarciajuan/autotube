"""Tests for the W4 channel niche guard (no network)."""

import os
import sys
from types import SimpleNamespace

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from pipeline.niche_guard import (  # noqa: E402
    filter_on_niche,
    is_guard_enabled,
    is_on_niche,
    niche_fit_score,
)
from pipeline.title_engine import score_title  # noqa: E402


def _medical_cfg(**overrides):
    base = dict(
        NICHE_GUARD_ENABLED=True,
        TITLE_NICHE_FIT_MIN=0.3,
        NICHE_ANCHORS=[
            "enfermedad", "sintom", "diagnostic", "paciente", "medic",
            "cerebro", "adn", "patolog", "anomal", "inmun",
        ],
        CHANNEL_KEYWORDS=["enfermedades raras", "casos clinicos"],
        SEO_SECONDARY_KEYWORDS=["diagnostico"],
        TITLE_GOOD_EXAMPLES=["El síndrome que nadie diagnosticó"],
        SEO_PRIMARY_KEYWORD="enfermedades raras documental",
        TITLE_TARGET_MIN_CHARS=45,
        TITLE_TARGET_MAX_CHARS=65,
        TITLE_MAX_CHARS=65,
        TITLE_CAPS_POLICY="sentence",
        TITLE_BANNED_PATTERNS=[],
        TITLE_REQUIRED_SPECIFICITY=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_guard_disabled_without_anchors():
    cfg = _medical_cfg(NICHE_ANCHORS=[])
    assert not is_guard_enabled(cfg)
    assert is_on_niche("Alejandro Magno conquista Persia", cfg)
    assert niche_fit_score("lo que sea", cfg) == 0.5


def test_off_niche_medical_channel():
    cfg = _medical_cfg()
    assert not is_on_niche("Alejandro Magno conquistó Persia", cfg)
    assert niche_fit_score("Alejandro Magno conquistó Persia", cfg) < 0.3


def test_on_niche_medical_channel():
    cfg = _medical_cfg()
    assert is_on_niche("El síndrome de Alicia: cuando el cerebro distorsiona", cfg)
    assert niche_fit_score("El síndrome de Alicia: cuando el cerebro distorsiona", cfg) >= 0.6


def test_filter_drops_off_niche_only_when_something_remains():
    cfg = _medical_cfg()
    items = [
        {"title": "Alejandro Magno conquistó Persia"},
        {"title": "El síndrome de Alicia y el cerebro"},
    ]
    kept, dropped = filter_on_niche(items, lambda i: i["title"], cfg)
    assert dropped == 1
    assert len(kept) == 1
    assert "síndrome" in kept[0]["title"]


def test_filter_never_starves_when_all_off_niche():
    cfg = _medical_cfg()
    items = [{"title": "Alejandro Magno"}, {"title": "La guerra de las Galias"}]
    kept, dropped = filter_on_niche(items, lambda i: i["title"], cfg)
    assert dropped == 0
    assert kept == items


def test_title_rubric_penalizes_off_niche():
    cfg = _medical_cfg()
    _score, breakdown = score_title("Alejandro Magno conquistó Persia en el año 331", {}, cfg)
    assert breakdown["penalties"].get("off_niche") == -8


def test_title_rubric_not_penalized_when_on_niche():
    cfg = _medical_cfg()
    _score, breakdown = score_title(
        "El síndrome de Alicia: el cerebro distorsiona el mundo real", {}, cfg
    )
    assert "off_niche" not in breakdown["penalties"]
