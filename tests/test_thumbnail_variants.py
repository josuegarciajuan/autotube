"""Tests for the W6 distinct A/B thumbnail strategies (no LLM/network)."""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from pipeline.thumbnail_brainstorm import (  # noqa: E402
    VARIANT_STRATEGIES,
    ThumbnailBrainstorm,
    ThumbnailBrief,
)


def test_three_strategies_with_distinct_layout_pools():
    assert set(VARIANT_STRATEGIES) == {"subject_hero", "data_question", "tension_contrast"}
    pools = [tuple(v["layouts"]) for v in VARIANT_STRATEGIES.values()]
    assert len(set(pools)) == 3
    for strategy in VARIANT_STRATEGIES.values():
        assert strategy["directive"] and strategy["persona"]


def test_clone_brief_carries_strategy_label():
    base = ThumbnailBrief(image_concept="base", layout="topic_hero")
    out = ThumbnailBrainstorm()._clone_brief_with_directive(
        base, "directiva de prueba", "canal", variant_strategy="data_question"
    )
    assert out.variant_strategy == "data_question"


def test_brainstorm_variants_returns_three_distinct_strategies(monkeypatch):
    br = ThumbnailBrainstorm()

    def fake_brainstorm(**kwargs):
        return ThumbnailBrief(image_concept="base", layout="topic_hero")

    def fake_strategy(key, **kwargs):
        return ThumbnailBrief(
            image_concept=key,
            layout=VARIANT_STRATEGIES[key]["layouts"][0],
            variant_strategy=key,
        )

    monkeypatch.setattr(br, "brainstorm", fake_brainstorm)
    monkeypatch.setattr(br, "_run_strategy_brief", fake_strategy)
    out = br.brainstorm_variants(script_text="", title="Titulo de prueba", num_variants=3)
    assert [b.variant_strategy for b in out] == [
        "subject_hero", "data_question", "tension_contrast",
    ]
    assert len({b.layout for b in out}) >= 2
