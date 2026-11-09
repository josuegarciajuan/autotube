"""Regression: recover LLM JSON with missing ':' / ',' (channel_analyzer)."""


def test_missing_colon_after_key_is_repaired():
    from pipeline_dist.llm_repair import repair_json_text

    repaired = repair_json_text('{"recommendations": [{"title" "x"}]}')
    assert repaired == {"recommendations": [{"title": "x"}]}


def test_missing_comma_between_value_and_key_is_repaired():
    from pipeline_dist.llm_repair import repair_json_text

    repaired = repair_json_text(
        '{"a": 1, "recommendations": [{"title": "x" "body": "y"}]}'
    )
    assert repaired == {
        "a": 1,
        "recommendations": [{"title": "x", "body": "y"}],
    }


def test_trailing_comma_and_smart_quotes_are_repaired():
    from pipeline_dist.llm_repair import repair_json_text

    repaired = repair_json_text('{\u201ctitle\u201d: \u201cx\u201d,}')
    assert repaired == {"title": "x"}


def test_invalid_text_returns_none():
    from pipeline_dist.llm_repair import repair_json_text

    assert repair_json_text("not json at all") is None
    assert repair_json_text("") is None
