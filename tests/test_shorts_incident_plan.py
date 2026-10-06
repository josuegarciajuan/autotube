"""Focused regression tests for the shorts incident remediation."""

from types import SimpleNamespace


def test_render_timeout_scales_with_duration_and_assets():
    from pipeline.shorts_media import has_sufficient_visual_assets, render_timeout_seconds

    short = render_timeout_seconds(audio_duration=20, asset_count=4)
    long = render_timeout_seconds(audio_duration=58, asset_count=12)

    assert short >= 180
    assert long > short
    assert long <= 1800
    assert has_sufficient_visual_assets([{"path": "a"}, None], 0.5) is True
    assert has_sufficient_visual_assets([None, None], 0.5) is False


def test_load_gate_blocks_shorts_only_when_longform_is_active_and_autotube_load_is_high():
    from api.services.shorts_scheduler import should_defer_shorts_for_longform_load

    # cpu_count=8 -> threshold = max(1, int(8*0.85)) = 6 autotube render procs
    assert should_defer_shorts_for_longform_load(
        longform_active=True, autotube_render_load=8, cpu_count=8
    ) is True
    assert should_defer_shorts_for_longform_load(
        longform_active=True, autotube_render_load=2, cpu_count=8
    ) is False
    assert should_defer_shorts_for_longform_load(
        longform_active=False, autotube_render_load=20, cpu_count=8
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


def test_media_phase_longform_defers_shorts():
    from api.services.shorts_scheduler import should_defer_shorts_for_longform_load

    # cpu_count=10 -> threshold=8. Two long-forms in the local-SD/media phase
    # pin every core, so each charges cpu//2=5: 2 + 10 >= 8 -> defer.
    assert should_defer_shorts_for_longform_load(
        longform_active=True, autotube_render_load=2, cpu_count=10,
        longform_media_jobs=2,
    ) is True
    # One media-phase long-form is below the bar: shorts may still coexist.
    assert should_defer_shorts_for_longform_load(
        longform_active=True, autotube_render_load=2, cpu_count=10,
        longform_media_jobs=1,
    ) is False


def test_pollinations_circuit_breaker_opens_on_402():
    from pipeline import media_fetcher as mf

    mf._POLLINATIONS_BREAK_UNTIL = 0.0
    assert mf._is_payment_required(RuntimeError("402 Client Error: Payment Required")) is True
    assert mf._is_payment_required(RuntimeError("500 Server Error")) is False
    assert mf._pollinations_circuit_open() is False
    mf._trip_pollinations_breaker(1800)
    assert mf._pollinations_circuit_open() is True
    mf._POLLINATIONS_BREAK_UNTIL = 0.0


def _seed_short_tables(db_path):
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE shorts_planned_slots ("
        "id INTEGER PRIMARY KEY, status TEXT, retry_count INTEGER, "
        "error_message TEXT, job_id INTEGER, short_id INTEGER, "
        "scheduled_at TIMESTAMP, updated_at TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE generation_jobs ("
        "id INTEGER PRIMARY KEY, status TEXT, error_msg TEXT, finished_at TIMESTAMP)"
    )
    conn.commit()
    conn.close()


def test_retry_preserves_real_short_failure_reason(tmp_path, monkeypatch):
    """A retry must not wipe the real reason with the generic 'no short_id'."""
    import sqlite3

    from config import settings
    from api.services import shorts_scheduler as ss

    db_path = tmp_path / "test.db"
    _seed_short_tables(db_path)
    monkeypatch.setattr(settings, "DATABASE_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO shorts_planned_slots "
        "(id, status, retry_count, error_message, job_id) VALUES (7, 'retrying', 0, ?, 42)",
        ("render híbrido falló: timeout after 1280s",),
    )
    conn.execute(
        "INSERT INTO generation_jobs (id, status, error_msg) "
        "VALUES (42, 'running', ?)",
        ("render híbrido falló: timeout after 1280s",),
    )
    conn.commit()
    conn.close()

    ss._finalize_short_dispatch(slot_id=7, job_id=42, channel_id=1, short_id=None, exc=None)

    conn = sqlite3.connect(str(db_path))
    slot = conn.execute(
        "SELECT error_message FROM shorts_planned_slots WHERE id=7"
    ).fetchone()
    job = conn.execute("SELECT error_msg FROM generation_jobs WHERE id=42").fetchone()
    conn.close()

    assert "render híbrido falló" in (slot[0] or "")
    assert "auto-retry" in (slot[0] or "")
    assert "render híbrido falló" in (job[0] or "")
