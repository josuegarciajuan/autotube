"""TDD contracts for channel-scoped enforcement evidence and recovery."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from api.services import channel_enforcement
from api.services import pacing_profile


class FakeDB:
    def __init__(self):
        self.state = {"pacing_profile": "normal", "channel_delivery_state_1": "strike",
                      "channel_delivery_state_2": "normal"}
        self.enforcement_events = []
        self.alerts = []
        self.channels = [
            {"id": 1, "slug": "canal1", "name": "One"},
            {"id": 2, "slug": "canal2", "name": "Two"},
        ]

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value

    def get_channels(self, active_only=False):
        return self.channels

    def get_channel(self, channel_id):
        return next((c for c in self.channels if c["id"] == channel_id), None)

    def get_channel_planning_config(self, channel_id):
        return {}

    def get_delivery_profile(self, state):
        return {"public_videos_per_day": {"strike": 1, "recovery": 1, "normal": 2}[state],
                "native_shorts_per_day": 1, "global_shorts_per_day": 6}


def test_unavailable_is_informational_and_does_not_enforce_or_change_pacing():
    db = FakeDB()
    result = channel_enforcement.record_delivery_event(
        db, channel_id=1, classification="unavailable",
        evidence={"source": "watch_page", "status": "unavailable"},
        source="yt_state_reconciler",
    )
    assert result["enforced"] is False
    assert result["alert_type"] == "channel_delivery_unavailable"
    assert db.get_system_state("shorts_spam_strikes_1") is None
    assert db.get_system_state("pacing_profile") == "normal"
    assert result["scope"] == "channel_id:1"


def test_confirmed_strike_requires_explicit_classification_and_evidence():
    db = FakeDB()
    result = channel_enforcement.record_delivery_event(
        db, channel_id=1, classification=None, evidence=None, source="watch_page"
    )
    assert result["enforced"] is False
    assert result["reason"] == "explicit_classification_and_evidence_required"
    assert db.get_system_state("shorts_spam_strikes_1") is None


def test_confirmed_strike_is_scoped_to_one_channel_and_never_resets_global_profile():
    db = FakeDB()
    result = channel_enforcement.record_delivery_event(
        db, channel_id=1, classification="confirmed_strike",
        evidence={"source": "studio", "case_id": "case-1"}, source="operator"
    )
    assert result["enforced"] is True
    assert result["scope"] == "channel_id:1"
    assert db.get_system_state("pacing_profile") == "normal"
    assert pacing_profile.get_active_profile_name(db) == "normal"
    assert db.get_system_state("shorts_spam_strikes_1") == "1"
    assert db.get_system_state("shorts_spam_strikes_2") is None


def test_auto_transition_uses_clean_days_per_channel():
    db = FakeDB()
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    db.enforcement_events = [
        {"channel_id": 1, "classification": "confirmed_strike", "occurred_at": old},
    ]
    result = channel_enforcement.auto_transition_channels(db)
    assert result["channels"]["1"]["to"] == "recovery"
    assert result["channels"]["2"]["to"] == "normal"
    assert result["scope"] == "per_channel"


def test_enforcement_payload_exposes_cause_source_timestamps_and_scope():
    db = FakeDB()
    payload = channel_enforcement.get_channel_enforcement(db, 1)
    assert {"state", "cause", "source", "occurred_at", "updated_at", "scope"} <= payload.keys()
    assert payload["scope"] == "channel_id:1"


def test_v52_imports_legacy_alerts_as_inferred_without_deleting_audit(tmp_path):
    from database.db_extended import _migrate_v52

    conn = sqlite3.connect(tmp_path / "migration.db")
    conn.executescript("""
        CREATE TABLE channels (id INTEGER PRIMARY KEY);
        CREATE TABLE pipeline_alerts (
            id INTEGER PRIMARY KEY, channel_id INTEGER, alert_type TEXT,
            metadata_json TEXT, created_at TEXT
        );
        INSERT INTO channels VALUES (1);
        INSERT INTO pipeline_alerts VALUES
            (7, 1, 'spam_strike', '{"legacy": true}', '2026-08-01 00:00:00');
    """)
    _migrate_v52(conn, __import__("logging").getLogger("test"))
    assert conn.execute("SELECT COUNT(*) FROM pipeline_alerts").fetchone()[0] == 1
    row = conn.execute("SELECT classification, enforced, source FROM channel_enforcement_events").fetchone()
    assert tuple(row) == ("inferred", 0, "legacy_pipeline_alerts")


def test_watch_page_removal_is_informational_even_after_two_confirmations():
    db = FakeDB()
    result = channel_enforcement.record_watch_page_observation(
        db, channel_id=1, video_id="yt-1", visibility="removed", confirmations=2,
        source="watch_page",
    )
    assert result["classification"] == "removal_confirmed"
    assert result["enforced"] is False
    assert result["alert_type"] != "spam_strike"
    assert db.get_system_state("shorts_spam_strikes_1") is None


def test_watch_page_single_removal_is_unconfirmed_and_informational():
    result = channel_enforcement.record_watch_page_observation(
        FakeDB(), channel_id=1, video_id="yt-2", visibility="removed",
        confirmations=1, source="watch_page",
    )
    assert result["classification"] == "video_removed_unconfirmed"
    assert result["enforced"] is False


def test_explicit_strike_accepts_only_operator_evidence_sources():
    db = FakeDB()
    rejected = channel_enforcement.record_confirmed_strike(
        db, channel_id=1, source="watch_page", evidence={"video_id": "yt-3"}
    )
    assert rejected["enforced"] is False
    assert rejected["reason"] == "confirmed_strike_source_not_allowed"
    accepted = channel_enforcement.record_confirmed_strike(
        db, channel_id=1, source="operator", evidence={"case_id": "studio-3"}
    )
    assert accepted["enforced"] is True
    assert db.get_system_state("shorts_spam_strikes_1") == "1"


def test_generic_event_boundary_rejects_watch_page_as_confirmed_strike():
    db = FakeDB()
    result = channel_enforcement.record_delivery_event(
        db, channel_id=1, classification="confirmed_strike",
        evidence={"video_id": "yt-4"}, source="watch_page",
    )
    assert result["enforced"] is False
    assert result["reason"] == "confirmed_strike_source_not_allowed"
