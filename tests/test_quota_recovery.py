"""Integration contracts for quota-breaker recovery."""

from datetime import datetime, timezone

from api import main


class RecoveryDatabase:
    def __init__(self, *, exhausted=True):
        self.exhausted = exhausted
        self.expired_reservations = 0
        self.cleared = 0

    def expire_youtube_quota_reservations(self):
        self.expired_reservations += 1
        return 2

    def is_quota_exhausted(self):
        return self.exhausted

    def get_quota_reset_time(self):
        return {
            "exhausted": True,
            "reset_at_utc": "2026-08-15T00:00:00+00:00",
        }

    def clear_quota_exhausted(self):
        self.cleared += 1
        self.exhausted = False


def test_recovery_expires_stale_reservations_and_only_clears_breaker_in_remediation(monkeypatch):
    db = RecoveryDatabase()
    dispatched = []
    monkeypatch.setattr(main, "YT_REMEDIATION_MODE", True)

    recovered = main._recover_quota_once(
        db,
        now_utc=datetime(2026, 8, 15, 0, 15, tzinfo=timezone.utc),
        dispatch_uploads=lambda: dispatched.append(True),
    )

    assert recovered is True
    assert db.expired_reservations == 1
    assert db.cleared == 1
    assert dispatched == []


def test_recovery_dispatches_backlog_only_after_remediation_is_disabled(monkeypatch):
    db = RecoveryDatabase()
    dispatched = []
    monkeypatch.setattr(main, "YT_REMEDIATION_MODE", False)

    recovered = main._recover_quota_once(
        db,
        now_utc=datetime(2026, 8, 15, 0, 15, tzinfo=timezone.utc),
        dispatch_uploads=lambda: dispatched.append(True),
    )

    assert recovered is True
    assert db.expired_reservations == 1
    assert db.cleared == 1
    assert dispatched == [True]


def test_recovery_expires_reservations_even_without_an_active_breaker(monkeypatch):
    db = RecoveryDatabase(exhausted=False)
    monkeypatch.setattr(main, "YT_REMEDIATION_MODE", True)

    recovered = main._recover_quota_once(db)

    assert recovered is False
    assert db.expired_reservations == 1
    assert db.cleared == 0
