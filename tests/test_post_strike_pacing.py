"""Contracts for the per-channel post-strike pacing policy."""

from datetime import datetime, timezone

from api.services import gradual_resume
from api.services import shorts_scheduler


class _ResumeDb:
    def __init__(self, plan):
        self.plan = plan

    def get_system_state(self, key):
        if key.startswith("resume_plan_"):
            return self.plan
        return ""

    def get_channels(self, active_only=True):
        return []


def test_phase_two_channel_receives_two_native_shorts_per_day():
    """A post-strike phase-2 channel gets its approved native-short entitlement."""
    db = _ResumeDb(
        '{"start_iso":"2026-08-01T00:00:00+00:00","source":"unblock","slug":"canal5"}'
    )

    effective = gradual_resume.effective_native_shorts_per_day(
        7, db, today=datetime(2026, 8, 25, tzinfo=timezone.utc).date()
    )

    assert effective == 2


def test_phase_one_channel_keeps_one_native_short_per_day():
    """The initial five-day post-strike ramp must not be relaxed."""
    db = _ResumeDb(
        '{"start_iso":"2026-08-23T00:00:00+00:00","source":"unblock","slug":"canal4"}'
    )

    effective = gradual_resume.effective_native_shorts_per_day(
        5, db, today=datetime(2026, 8, 25, tzinfo=timezone.utc).date()
    )

    assert effective == 1


def test_phase_two_cap_allows_a_second_native_short_today(monkeypatch, tmp_path):
    """The upload valve's hard cap must honor the phase-2 entitlement."""
    from tests.test_shorts_queue_unified import _db, _set_published
    import config.settings as settings

    db, path = _db(tmp_path)
    monkeypatch.setattr(settings, "DATABASE_PATH", __import__("pathlib").Path(path))
    _set_published(db, 1, 1)
    monkeypatch.setattr(
        gradual_resume, "effective_native_shorts_per_day", lambda *args, **kwargs: 2,
    )

    assert shorts_scheduler._channel_hard_daily_short_cap_reached(1, db) is False
