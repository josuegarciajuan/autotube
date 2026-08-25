"""Focused regression tests for the shorts incident remediation."""

from types import SimpleNamespace


def test_render_timeout_scales_with_duration_and_assets():
    from pipeline.shorts_media import has_sufficient_visual_assets, render_timeout_seconds

    short = render_timeout_seconds(audio_duration=20, asset_count=4)
    long = render_timeout_seconds(audio_duration=58, asset_count=12)

    assert short >= 180
    assert long > short
    assert long <= 900
    assert has_sufficient_visual_assets([{"path": "a"}, None], 0.5) is True
    assert has_sufficient_visual_assets([None, None], 0.5) is False


def test_load_gate_blocks_shorts_only_when_longform_is_active_and_load_is_high():
    from api.services.shorts_scheduler import should_defer_shorts_for_longform_load

    assert should_defer_shorts_for_longform_load(
        longform_active=True, load1=8.0, cpu_count=8
    ) is True
    assert should_defer_shorts_for_longform_load(
        longform_active=True, load1=2.0, cpu_count=8
    ) is False
    assert should_defer_shorts_for_longform_load(
        longform_active=False, load1=20.0, cpu_count=8
    ) is False


def test_voice_aware_word_budget_leaves_safety_margin():
    from pipeline.shorts_tts import voice_aware_word_budget

    base = voice_aware_word_budget(58, rate="-10%", block_count=6)
    slow = voice_aware_word_budget(58, rate="-30%", block_count=6)

    assert 45 <= slow < base <= 105


def test_standalone_topic_selection_skips_unsafe_topic_without_disabling_safety():
    from api.services.shorts_scheduler import select_safe_standalone_topic
    from pipeline.content_safety import SafetyVerdict

    rejected = []

    def classify(topic):
        if topic["title"] == "unsafe":
            return SafetyVerdict(False, "blocked", ["true_crime"])
        return SafetyVerdict(True)

    selected = select_safe_standalone_topic(
        [{"title": "unsafe"}, {"title": "safe"}],
        classify=classify,
        on_reject=rejected.append,
    )

    assert selected["title"] == "safe"
    assert rejected == ["blocked"]


def test_transient_short_outcomes_are_non_terminal():
    from api.services.shorts_scheduler import short_job_status_for_outcome

    assert short_job_status_for_outcome("retry") == "retrying"
    assert short_job_status_for_outcome("pacing") == "deferred"
    assert short_job_status_for_outcome("quota") == "deferred"
    assert short_job_status_for_outcome("terminal") == "failed"
