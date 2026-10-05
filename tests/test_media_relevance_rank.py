"""Fase 4a — action/context-aware media search and single-score ranking.

Covered invariants:
  * the narrated action/context dominates a generic label coincidence;
  * a "better" provider never beats relevance (the scorer ignores providers);
  * ``must_avoid`` is a strong penalty;
  * timeless scenes carry no era anchor; historical scenes carry it in EVERY
    query variant;
  * ``_simplify_query`` keeps the action verb;
  * ``_try_download_best_candidate`` respects ``rank_candidates`` (no double
    re-order / re-score);
  * the explicit fallback ladder order and the generic-fallback cap.

Run:  python3 -m pytest tests/test_media_relevance_rank.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import MagicMock

from pipeline.cinematic_staging import (
    FALLBACK_LADDER,
    GenericFallbackTracker,
    build_fallback_ladder,
    candidate_matches_action,
    classify_query_tier,
    is_generic_tier,
    rank_candidates,
    score_candidate,
)
from pipeline.scene_context import SceneVisualContext
from pipeline.theme_extractor import ThemeContext


# ═══════════════════════════════════════════════════════════════════
# Deterministic scoring / ranking
# ═══════════════════════════════════════════════════════════════════

def _action_ctx():
    return SceneVisualContext(
        subject="explorer",
        action="crossing dunes",
        object="compass",
        setting="Sahara desert",
        depiction_mode="literal",
    )


def test_action_dominates_generic_match():
    ctx = _action_ctx()
    generic = {"title": "desert panorama dunes", "tags": ["desert", "dunes"]}
    action = {
        "title": "explorer crossing dunes with compass",
        "tags": ["explorer", "crossing", "dunes", "compass"],
    }

    assert score_candidate(action, "explorer crossing dunes", None, ctx) > \
        score_candidate(generic, "explorer crossing dunes", None, ctx)

    ranked = rank_candidates([generic, action], "explorer crossing dunes", None, ctx)
    assert ranked[0] is action


def test_action_boost_can_be_disabled():
    ctx = _action_ctx()
    action = {"title": "explorer crossing dunes", "tags": ["explorer", "crossing", "dunes"]}
    with_boost = score_candidate(action, "", None, ctx, action_scene_boost=True)
    without = score_candidate(action, "", None, ctx, action_scene_boost=False)
    assert with_boost > without


def test_provider_does_not_beat_relevance():
    """A candidate from a 'premium' provider must not win on provider alone."""
    ctx = SceneVisualContext(subject="archivist", action="examining documents")
    relevant_unknown = {
        "title": "archivist examining documents",
        "tags": ["archivist", "documents"],
        "source": "unknown_provider",
    }
    generic_premium = {
        "title": "empty institutional building",
        "tags": ["building"],
        "source": "pexels",
    }

    ranked = rank_candidates(
        [generic_premium, relevant_unknown],
        "archivist examining documents",
        None,
        ctx,
    )
    assert ranked[0] is relevant_unknown


def test_must_avoid_penalizes_candidate():
    ctx = SceneVisualContext(subject="castle", must_avoid=["cartoon"])
    clean = {"title": "medieval stone castle", "tags": ["castle"]}
    forbidden = {"title": "cartoon castle", "tags": ["castle", "cartoon"]}

    assert score_candidate(forbidden, "medieval castle", None, ctx) < \
        score_candidate(clean, "medieval castle", None, ctx)

    ranked = rank_candidates([forbidden, clean], "medieval castle", None, ctx)
    assert ranked[0] is clean


def test_no_metadata_is_neutral_not_bad():
    ctx = _action_ctx()
    empty = {"title": "", "tags": []}
    assert score_candidate(empty, "explorer crossing dunes", None, ctx) == 0.0


def test_rank_drops_historical_anachronism_but_honours_scene_era():
    theme = ThemeContext(era="17th century", era_decade="17th century")
    modern = {"title": "modern city skyline", "tags": ["modern", "city"]}
    period = {"title": "wooden sailing ship at sea", "tags": ["sailing", "ship"]}

    ranked = rank_candidates([modern, period], "sailing ship", theme)
    assert ranked == [period]

    # A per-scene non-historical era (Fase 3 deliberate jump) must NOT drop it.
    modern_ctx = SceneVisualContext(era="actualidad")
    ranked_jump = rank_candidates([modern, period], "excavation", theme, modern_ctx)
    assert modern in ranked_jump


def test_candidate_matches_action_fail_open():
    assert candidate_matches_action({"title": "x"}, None) is True
    assert candidate_matches_action({"title": "x"}, SceneVisualContext()) is True
    ctx = _action_ctx()
    assert candidate_matches_action(
        {"title": "explorer crossing dunes"}, ctx
    ) is True
    assert candidate_matches_action(
        {"title": "static map on a wall"}, ctx
    ) is False


# ═══════════════════════════════════════════════════════════════════
# Fallback ladder + generic-fallback accounting
# ═══════════════════════════════════════════════════════════════════

def test_fallback_ladder_order_dedup_and_fit():
    tiers = {
        "symbolic": ["generic documentary establishing shot"],
        "context": ["context establishing village"],
        "action_compatible": ["explorer crossing dunes"],
        "action_exact": ["explorer crossing dunes", "explorer crossing dunes with compass"],
    }
    ladder = build_fallback_ladder(tiers, max_len=100)

    assert ladder[0] == "explorer crossing dunes"
    assert ladder[1] == "explorer crossing dunes with compass"
    assert ladder[2] == "context establishing village"
    assert ladder[3] == "generic documentary establishing shot"
    assert len(ladder) == len(set(ladder))
    assert all(len(q) <= 100 for q in ladder)


def test_fallback_ladder_is_fail_open_on_bad_input():
    assert build_fallback_ladder(None) == []
    assert build_fallback_ladder("not a dict") == []
    assert build_fallback_ladder({"unknown": ["x"], "action_exact": [""]}) == ["x"]


def test_generic_fallback_tracker_warns_only_above_cap():
    tracker = GenericFallbackTracker(max_pct=20.0)
    for _ in range(8):
        tracker.record("action_exact")
    for _ in range(2):
        tracker.record("symbolic")
    assert tracker.generic_pct == 20.0
    assert tracker.exceeds() is False

    tracker.record("context")  # 3/11 ≈ 27.3% > 20%
    assert tracker.exceeds() is True
    assert is_generic_tier("context") and is_generic_tier("symbolic")
    assert not is_generic_tier("action_exact")


def test_generic_fallback_tracker_tolerant_bad_cap():
    assert GenericFallbackTracker(max_pct="nope").max_pct == 20.0
    assert GenericFallbackTracker(max_pct=999).max_pct == 100.0


def test_classify_query_tier():
    ctx = _action_ctx()
    assert classify_query_tier("explorer crossing dunes", ctx) == "action_exact"
    assert classify_query_tier("sailors sailing the sea", None) == "action_compatible"
    assert classify_query_tier("historical documentary establishing", None) == "context"
    assert classify_query_tier("quiet empty room", None) == "symbolic"


def test_fallback_ladder_constant_order():
    assert FALLBACK_LADDER == (
        "action_exact", "action_compatible", "context", "symbolic",
    )


# ═══════════════════════════════════════════════════════════════════
# MediaFetcher integration
# ═══════════════════════════════════════════════════════════════════

def _fetcher(**strategy):
    from pipeline.media_fetcher import MediaFetcher

    f = object.__new__(MediaFetcher)
    base = {
        "era_anchor_enabled": True,
        "relevance_min_overlap": 1,
        "llm_relevance_filter": False,
        "action_scene_boost": True,
        "require_action_match": False,
        "max_generic_fallback_pct": 20,
        "fallback_queries": [],
    }
    base.update(strategy)
    f._media_strategy = base
    f._config = {}
    f._theme_context = None
    f._visual_bible = None
    f._is_asset_duplicate = lambda c: False
    f._is_anachronistic = lambda c, ctx: False
    f._download_candidate = lambda provider, c: {"path": c.get("title")}
    f._record_asset_used = lambda c: None
    f._record_asset_for_history = lambda d: None
    return f


def test_try_download_respects_rank_order_without_rescoring():
    """The unified rank order must NOT be re-sorted by _relevance_score."""
    f = _fetcher()
    # Sabotage: the legacy relevance would strongly prefer the generic label.
    f._relevance_score = lambda candidate, scene, ctx: (
        100.0 if "generic" in candidate["title"] else 0.0
    )
    scene = {
        "search_query_en": "explorer crossing dunes",
        "texto": "The explorer crosses the dunes.",
        "scene_idx": 0,
    }
    candidates = [
        {"title": "generic desert label", "tags": ["desert"]},
        {"title": "explorer crossing dunes", "tags": ["explorer", "crossing", "dunes"]},
    ]

    result = f._try_download_best_candidate(candidates, object(), scene, ThemeContext())

    assert result is not None
    assert result["path"] == "explorer crossing dunes"


def test_require_action_match_prefers_action_candidate():
    f = _fetcher(require_action_match=True)
    scene = {
        "search_query_en": "explorer crossing dunes",
        "texto": "The explorer crosses the dunes.",
        "scene_idx": 0,
    }
    candidates = [
        {"title": "empty desert panorama", "tags": ["desert"]},
        {"title": "explorer crossing dunes", "tags": ["explorer", "crossing", "dunes"]},
    ]

    result = f._try_download_best_candidate(candidates, object(), scene, ThemeContext())
    assert result["path"] == "explorer crossing dunes"


def test_require_action_match_is_last_resort_when_nothing_matches():
    f = _fetcher(require_action_match=True)
    scene = {
        "search_query_en": "explorer crossing dunes",
        "texto": "The explorer crosses the dunes.",
        "scene_idx": 0,
    }
    candidates = [
        {"title": "empty desert panorama", "tags": ["desert"]},
        {"title": "static map on a wall", "tags": ["map"]},
    ]

    result = f._try_download_best_candidate(candidates, object(), scene, ThemeContext())
    # No candidate matches the action → full list kept (never blocks).
    assert result is not None


def test_simplify_query_keeps_action_verb():
    from pipeline.media_fetcher import MediaFetcher

    simplified = MediaFetcher._simplify_query(
        "explorer crossing desert at night", max_keywords=2
    )
    assert "crossing" in simplified

    wide = MediaFetcher._simplify_query("explorer crossing desert at night")
    assert "crossing" in wide

    # No action verb → previous behavior (first N keywords).
    assert MediaFetcher._simplify_query(
        "ancient egyptian temple columns", max_keywords=2
    ) == "ancient egyptian"


def test_query_pool_first_variant_is_action_exact():
    f = _fetcher()
    scene = {
        "search_query_en": "sailors navigating storm",
        "texto": "The sailors navigate through the storm.",
        "tipo": "desarrollo",
    }
    ctx = ThemeContext(
        era="17th century", era_decade="17th century",
        primary_subject="sailing ship", theme_keywords_en=["sailing", "ship"],
    )

    pool = f._build_query_pool(scene, ctx, scene_idx=0)

    assert pool
    assert "navigating" in pool[0].lower() or "storm" in pool[0].lower()


def test_historical_scene_era_anchor_in_every_variant():
    f = _fetcher()
    scene = {
        "search_query_en": "sailors navigating storm",
        "texto": "The sailors navigate through the storm.",
        "tipo": "desarrollo",
    }
    ctx = ThemeContext(
        era="17th century", era_decade="17th century",
        primary_subject="sailing ship", theme_keywords_en=["sailing", "ship"],
    )
    era_phrase = "17th century wooden sailing ship"

    pool = f._build_query_pool(scene, ctx, scene_idx=0)

    assert pool
    assert all(era_phrase in q for q in pool), [
        q for q in pool if era_phrase not in q
    ]
    # No duplicated partial era fragment ("17th 17th century…").
    assert not any("17th 17th" in q for q in pool)


def test_timeless_scene_has_no_era_anchor():
    f = _fetcher()
    scene = {
        "search_query_en": "city skyline modern",
        "texto": "A modern city at dusk.",
        "tipo": "desarrollo",
    }
    ctx = ThemeContext(era="presente", era_decade="")

    pool = f._build_query_pool(scene, ctx, scene_idx=0)

    assert pool
    assert not any("presente" in q.lower() for q in pool)
    # No historical anchor either.
    assert not any("17th century" in q for q in pool)


def test_fase3_anachronism_exemption_is_honoured():
    """A deliberate modern segment must not veto modern footage."""
    f = _fetcher()
    f._is_anachronistic = lambda candidate, ctx: True  # global historical veto
    modern = {"title": "modern city excavation"}

    assert f._is_anachronistic_for_scene(
        modern, None, SceneVisualContext(era="actualidad")
    ) is False
    assert f._is_anachronistic_for_scene(
        modern, None, SceneVisualContext(era="17th century")
    ) is True
    assert f._is_anachronistic_for_scene(
        modern, None, SceneVisualContext(era="")
    ) is True


def test_generic_fallback_warning_is_advisory(caplog):
    f = _fetcher(max_generic_fallback_pct=0)
    f._reset_fallback_tracker()
    for _ in range(3):
        f._record_fallback_tier("documentary establishing shot")

    assert f._fallback_tracker.generic == 3
    with caplog.at_level("WARNING", logger="pipeline.media_fetcher"):
        f._warn_generic_fallback()
    assert "Generic fallback" in caplog.text
