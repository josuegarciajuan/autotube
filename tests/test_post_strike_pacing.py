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


def test_explicit_delivery_policy_overrides_the_automatic_resume_phase():
    """An operator-approved policy wins over the date-derived phase ramp."""
    db = _ResumeDb(
        '{"start_iso":"2026-08-23T00:00:00+00:00","source":"unblock","slug":"canal4"}'
    )
    db.get_system_state = lambda key: (
        '{"mode":"explicit","longs_per_day":1,"native_shorts_per_day":2,'
        '"shorts_enabled":true,"clips_enabled":false}'
        if key == "channel_delivery_policy_5"
        else db.plan
    )

    policy = gradual_resume.effective_delivery_policy(5, db)

    assert policy["longs_per_day"] == 1
    assert policy["native_shorts_per_day"] == 2
    assert policy["shorts_enabled"] is True


def test_explicit_delivery_policy_can_keep_struck_channel_at_zero():
    """An explicit zero policy blocks generation and upload after block expiry."""
    db = _ResumeDb("")
    db.get_system_state = lambda key: (
        '{"mode":"explicit","longs_per_day":0,"native_shorts_per_day":0,'
        '"shorts_enabled":false,"clips_enabled":false}'
        if key == "channel_delivery_policy_4"
        else ""
    )

    policy = gradual_resume.effective_delivery_policy(4, db)

    assert policy["longs_per_day"] == 0
    assert policy["native_shorts_per_day"] == 0
    assert policy["shorts_enabled"] is False


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


def test_explicit_policy_cap_is_exact_and_not_diluted_by_global_profile(monkeypatch, tmp_path):
    """A policy of one native/day must stay at one even if the global profile relaxes."""
    from tests.test_shorts_queue_unified import _db, _set_published
    import config.settings as settings

    db, path = _db(tmp_path)
    monkeypatch.setattr(settings, "DATABASE_PATH", __import__("pathlib").Path(path))
    _set_published(db, 1, 1)
    monkeypatch.setattr(
        shorts_scheduler, "_pacing_int", lambda *a, **kw: 3,  # global normal = 3/día
    )
    monkeypatch.setattr(
        gradual_resume, "get_explicit_delivery_policy",
        lambda *a, **kw: {"mode": "explicit", "longs_per_day": 1,
                          "native_shorts_per_day": 1, "shorts_enabled": True,
                          "clips_enabled": False},
    )

    assert shorts_scheduler._channel_hard_daily_short_cap_reached(1, db) is True


def test_explicit_zero_policy_blocks_upload_even_without_spam_block(monkeypatch, tmp_path):
    """A struck channel with zero native quota stays at zero after block expiry."""
    from tests.test_shorts_queue_unified import _db, _seed_queued, fake_uploader, patch_database_path
    import config.settings as settings

    db, path = _db(tmp_path)
    monkeypatch.setattr(settings, "DATABASE_PATH", __import__("pathlib").Path(path))
    _seed_queued(db, 1, 1)
    monkeypatch.setattr(
        shorts_scheduler, "_channel_shorts_spam_blocked", lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        gradual_resume, "get_explicit_delivery_policy",
        lambda *a, **kw: {"mode": "explicit", "longs_per_day": 0,
                          "native_shorts_per_day": 0, "shorts_enabled": False,
                          "clips_enabled": False},
    )

    uploaded = shorts_scheduler._upload_queued_shorts(db, max_per_pass=10)

    assert uploaded == 0
    with db._connect() as conn:
        row = conn.execute("SELECT status FROM shorts WHERE channel_id = 1").fetchone()
        assert row["status"] == "generated"
