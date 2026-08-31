"""Contract tests for the central per-channel policy resolver (phase 1)."""

from datetime import datetime, timezone

from api.services.channel_policy import (
    collect_channel_policy_snapshot,
    resolve_channel_policy,
    removal_is_confirmed,
    should_create_removal_alert,
)


class FakeDB:
    def __init__(self, state=None):
        self.state = state or {}

    def get_system_state(self, key):
        return self.state.get(key)

    def get_channel(self, channel_id):
        return {"id": channel_id, "slug": f"canal{channel_id}", "name": "Canal"}

    def get_channel_planning_config(self, channel_id):
        return self.state.get(f"config_{channel_id}", {})


def test_historical_strikes_are_kept_but_expired_block_does_not_block_channel():
    now = datetime(2026, 8, 31, tzinfo=timezone.utc).timestamp()
    db = FakeDB({
        "shorts_spam_strikes_4": "3",
        "shorts_spam_blocked_until_4": str(now - 60),
    })

    policy = resolve_channel_policy(4, db=db, now=now)

    assert policy["historical_strikes"] == 3
    assert policy["blocked"] is False
    assert policy["strike_active"] is False
    assert policy["blocked_until"] == now - 60


def test_active_block_still_blocks_channel_without_erasing_history():
    now = 1_000.0
    db = FakeDB({
        "shorts_spam_strikes_2": "1",
        "shorts_spam_blocked_until_2": "1600",
    })

    policy = resolve_channel_policy(2, db=db, now=now)

    assert policy["historical_strikes"] == 1
    assert policy["blocked"] is True
    assert policy["strike_active"] is True


def test_visibility_alert_requires_two_confirmed_removal_signals():
    assert removal_is_confirmed("removed", confirmations=1) is False
    assert removal_is_confirmed("removed", confirmations=2) is True
    assert should_create_removal_alert("private", confirmations=3) is False
    assert should_create_removal_alert("login_required", confirmations=3) is False
    assert should_create_removal_alert("unknown", confirmations=3) is False
    assert should_create_removal_alert("error", confirmations=3) is False


def test_explicit_removal_signal_is_not_replaced_by_generic_unavailable_error():
    assert removal_is_confirmed("unavailable", confirmations=2) is False
    assert removal_is_confirmed("removed", confirmations=2) is True


def test_snapshot_is_read_only_and_keeps_each_channel_policy():
    db = FakeDB({"shorts_spam_strikes_2": "2"})
    db.get_channels = lambda active_only=False: [
        {"id": 2, "slug": "canal2", "name": "Sincronías"},
        {"id": 3, "slug": "canal3", "name": "Civilizaciones Olvidadas"},
    ]

    snapshot = collect_channel_policy_snapshot(db, now=1000.0)

    assert [item["slug"] for item in snapshot] == ["canal2", "canal3"]
    assert snapshot[0]["historical_strikes"] == 2
    assert all("config" not in item for item in snapshot)


def test_channel_delivery_values_are_capped_by_global_profile():
    """Channel DB policy may tighten pacing, never loosen the active profile."""
    db = FakeDB({
        "pacing_profile": "strike",
        "config_7": {
            "MAX_LONGFORM_PUBLISH_PER_DAY": 3,
            "MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS": 2,
            "MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS": 2,
            "PUBLISH_TARGET_HOUR": 19,
            "PUBLISH_JITTER_MIN": 11,
        },
    })

    policy = resolve_channel_policy(7, db=db, now=1000)

    assert policy["longform_publish_cap"] == 1
    assert policy["same_channel_publish_gap_h"] == 24
    assert policy["same_channel_upload_gap_h"] == 6
    assert policy["publish_target_hour"] == 19
    assert policy["publish_window_spread_min"] == 11


def test_channel_policy_preserves_more_restrictive_db_values():
    db = FakeDB({"config_8": {
        "MAX_LONGFORM_PUBLISH_PER_DAY": 1,
        "MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS": 48,
        "MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS": 12,
        "PUBLISH_WINDOW_SPREAD_MIN": 17,
    }})

    policy = resolve_channel_policy(8, db=db, now=1000)

    assert policy["longform_publish_cap"] == 1
    assert policy["same_channel_publish_gap_h"] == 48
    assert policy["same_channel_upload_gap_h"] == 12
    assert policy["publish_window_spread_min"] == 17
