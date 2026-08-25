"""Focused regression tests for grounded cinematic scene staging."""

from pipeline.cinematic_staging import (
    build_contextual_fallback,
    build_scene_brief,
    rank_candidates,
    sanitize_person_query,
    sanitize_shot_direction,
)
from pipeline.theme_extractor import ThemeContext


def _ctx(**overrides):
    values = dict(
        genre="historical",
        era="ancient",
        era_decade="ancient",
        primary_subject="ancient Egyptian civilization",
        theme_keywords_en=["ancient", "Egyptian", "historical"],
        key_motifs=["papyrus", "temple"],
    )
    values.update(overrides)
    return ThemeContext(**values)


def test_ancient_egypt_money_becomes_historical_exchange():
    brief = build_scene_brief("merchants discuss money in ancient Egypt", theme_ctx=_ctx())

    assert "barter" in brief or "exchange" in brief
    assert "modern currency" not in brief
    assert "credit card" not in brief


def test_sixteenth_century_expedition_uses_period_vessel():
    ctx = _ctx(era="16th century", era_decade="16th century", primary_subject="maritime expedition")
    brief = build_scene_brief("expedition crosses the Atlantic", theme_ctx=ctx)

    assert "caravel" in brief or "wooden sailing vessel" in brief
    assert "modern ship" not in brief


def test_archive_investigation_contains_observable_archivist_action():
    brief = build_scene_brief("archive investigation", theme_ctx=ThemeContext(primary_subject="historical archive"))

    assert any(action in brief for action in ("archivist", "examining documents", "turning pages", "cataloguing"))


def test_person_direction_never_requests_close_up():
    direction = sanitize_shot_direction("close-up detail", has_person=True)

    assert "close-up" not in direction
    assert "medium shot" in direction or "wide shot" in direction


def test_person_query_removes_close_up_language():
    query = sanitize_person_query("archivist close-up examining documents")

    assert "close-up" not in query
    assert "medium shot" in query


def test_contextual_fallback_preserves_era_and_subject():
    fallback = build_contextual_fallback("desarrollo", _ctx())

    assert "ancient" in fallback.lower()
    assert "egypt" in fallback.lower()
    assert "cinematic" not in fallback.lower() or len(fallback.split()) > 2


def test_rank_candidates_rejects_anachronism_and_prefers_period_match():
    ctx = _ctx(era="16th century", era_decade="16th century")
    candidates = [
        {"title": "modern cruise ship expedition", "tags": ["ship", "expedition"]},
        {"title": "wooden caravel at sea", "tags": ["expedition", "sailing"]},
    ]

    ranked = rank_candidates(candidates, "expedition", ctx)

    assert ranked[0]["title"] == "wooden caravel at sea"
    assert all("modern cruise" not in c["title"] for c in ranked)
