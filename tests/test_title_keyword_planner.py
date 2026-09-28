"""Tests for the W2 zero-quota search-demand planner (no network)."""

import os
import sys
from types import SimpleNamespace

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pipeline.title_keyword_planner as planner_mod  # noqa: E402
from pipeline.title_keyword_planner import KeywordPlan, TitleKeywordPlanner  # noqa: E402
from pipeline.title_engine import score_title  # noqa: E402


def _cfg(**overrides):
    base = dict(
        CANAL_NAME="canal4",
        SEO_PRIMARY_KEYWORD="expediciones",
        SEO_SECONDARY_KEYWORDS=["naufragio", "hielo", "artico"],
        CHANNEL_KEYWORDS=["expedicion", "naufragio", "supervivencia"],
        TITLE_KEYWORD_MAX_SEEDS=6,
        TITLE_KEYWORD_COMPETITION_ENABLED=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_seeds_expand_with_intent_prefixes_and_suffixes():
    cfg = _cfg()
    planner = TitleKeywordPlanner(cfg, "canal4")
    seeds = planner._seeds({"keywords": '["expedicion franklin"]'}, None)
    assert seeds
    joined = " | ".join(seeds)
    assert "expedicion franklin" in joined
    assert any(s.startswith("cómo") or s.startswith("por qué") for s in seeds)


def test_plan_ranks_repeated_suggestion_higher(monkeypatch):
    cfg = _cfg()
    planner = TitleKeywordPlanner(cfg, "canal4")

    def fake_fetch(query, timeout=4.0):
        return ["expedicion franklin hielo", "expedicion franklin hielo", "otra cosa"]

    monkeypatch.setattr(planner_mod, "fetch_suggestions", fake_fetch)
    plan = planner.plan({"keywords": '["expedicion franklin"]'}, None)
    assert isinstance(plan, KeywordPlan)
    assert plan.primary
    assert plan.source in {"autocomplete", "seed"}


def test_plan_fails_open_when_network_empty(monkeypatch):
    cfg = _cfg()
    planner = TitleKeywordPlanner(cfg, "canal4")
    monkeypatch.setattr(planner_mod, "fetch_suggestions", lambda q, timeout=4.0: [])
    plan = planner.plan({"keywords": '["expedicion franklin"]'}, None)
    # Never empty: falls back to the raw seed.
    assert plan.primary
    assert plan.source in {"autocomplete", "seed", "none"}


def test_niche_fit_uses_channel_anchors():
    cfg = _cfg()
    planner = TitleKeywordPlanner(cfg, "canal4")
    assert planner._niche_fit("naufragio en el hielo") > planner._niche_fit("receta de cocina")


# ── Rubric integration ───────────────────────────────────────────────

_RUBRIC_CFG = SimpleNamespace(
    TITLE_TARGET_MIN_CHARS=45,
    TITLE_TARGET_MAX_CHARS=65,
    TITLE_MAX_CHARS=65,
    TITLE_CAPS_POLICY="sentence",
    TITLE_BANNED_PATTERNS=[],
    TITLE_REQUIRED_SPECIFICITY=[],
    SEO_PRIMARY_KEYWORD="",
)


def test_rubric_search_intent_front_loaded_scores_three():
    plan = KeywordPlan(primary="expedicion franklin")
    _score, breakdown = score_title(
        "La expedición Franklin 129 hombres desaparecieron en el hielo en 1845",
        {}, _RUBRIC_CFG, plan,
    )
    assert breakdown["search_intent"] == 3
    assert "keyword_absent" not in breakdown["penalties"]


def test_rubric_penalizes_absent_planned_query():
    plan = KeywordPlan(primary="sindrome de alicia")
    _score, breakdown = score_title(
        "La expedición Franklin 129 hombres desaparecieron en el hielo en 1845",
        {}, _RUBRIC_CFG, plan,
    )
    assert breakdown["search_intent"] == 0
    assert breakdown["penalties"].get("keyword_absent") == -4


def test_rubric_without_plan_is_neutral():
    _score, breakdown = score_title(
        "La expedición Franklin 129 hombres desaparecieron en el hielo en 1845",
        {}, _RUBRIC_CFG,
    )
    assert breakdown["search_intent"] == 1
    assert "keyword_absent" not in breakdown["penalties"]
