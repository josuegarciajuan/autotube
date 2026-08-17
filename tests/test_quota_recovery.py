"""Integration contracts for per-project quota-breaker recovery."""

from datetime import datetime, timezone

from api import main


class RecoveryDatabase:
    """Fake DB tracking which projects got cleared (per-project contract)."""

    def __init__(self, *, exhausted_projects=None, resets=None):
        self.exhausted_projects = list(exhausted_projects or [])
        self.resets = dict(resets or {})       # project -> reset_at_utc ISO str
        self.expired_reservations = 0
        self.cleared: list = []

    def expire_youtube_quota_reservations(self):
        self.expired_reservations += 1
        return 2

    def get_exhausted_projects(self):
        return list(self.exhausted_projects)

    def get_quota_reset_time(self, project_id=""):
        if project_id:
            if project_id not in self.exhausted_projects:
                return {"exhausted": False, "reset_at_utc": None}
            return {
                "exhausted": True,
                "reset_at_utc": self.resets.get(
                    project_id, "2026-08-15T00:00:00+00:00"
                ),
            }
        return {
            "exhausted": bool(self.exhausted_projects),
            "reset_at_utc": (
                self.resets.get(self.exhausted_projects[0])
                if self.exhausted_projects else None
            ),
        }

    def clear_quota_exhausted(self, project_id=""):
        self.cleared.append(project_id)
        if project_id and project_id in self.exhausted_projects:
            self.exhausted_projects.remove(project_id)
        elif not project_id:
            self.exhausted_projects = []


def test_recovery_expires_stale_reservations_and_only_clears_breaker_in_remediation(monkeypatch):
    db = RecoveryDatabase(
        exhausted_projects=["youtube-uploads-automation"],
        resets={"youtube-uploads-automation": "2026-08-15T00:00:00+00:00"},
    )
    dispatched = []
    monkeypatch.setattr(main, "YT_REMEDIATION_MODE", True)

    recovered = main._recover_quota_once(
        db,
        now_utc=datetime(2026, 8, 15, 0, 16, tzinfo=timezone.utc),
        dispatch_uploads=lambda: dispatched.append(True),
    )

    assert recovered == ["youtube-uploads-automation"]
    assert db.expired_reservations == 1
    assert db.cleared == ["youtube-uploads-automation"]
    assert dispatched == []


def test_recovery_dispatches_backlog_only_after_remediation_is_disabled(monkeypatch):
    db = RecoveryDatabase(
        exhausted_projects=["youtube-uploads-automation"],
        resets={"youtube-uploads-automation": "2026-08-15T00:00:00+00:00"},
    )
    dispatched = []
    monkeypatch.setattr(main, "YT_REMEDIATION_MODE", False)

    recovered = main._recover_quota_once(
        db,
        now_utc=datetime(2026, 8, 15, 0, 16, tzinfo=timezone.utc),
        dispatch_uploads=lambda: dispatched.append(True),
    )

    assert recovered == ["youtube-uploads-automation"]
    assert db.cleared == ["youtube-uploads-automation"]
    assert dispatched == [True]


def test_recovery_expires_reservations_even_without_an_active_breaker(monkeypatch):
    db = RecoveryDatabase(exhausted_projects=[])
    monkeypatch.setattr(main, "YT_REMEDIATION_MODE", True)

    recovered = main._recover_quota_once(db)

    assert recovered == []
    assert db.expired_reservations == 1
    assert db.cleared == []


def test_recovery_is_independent_per_project():
    """Project A reached its reset → cleared. Project B still within its
    safety buffer → stays blocked. A single account recovering must not
    unblock the other one."""
    db = RecoveryDatabase(
        exhausted_projects=["youtube-uploads-automation", "autotube-expediciones"],
        resets={
            "youtube-uploads-automation": "2026-08-15T00:00:00+00:00",  # reset reached
            "autotube-expediciones": "2026-08-15T00:30:00+00:00",       # not yet
        },
    )

    recovered = main._recover_quota_once(
        db,
        now_utc=datetime(2026, 8, 15, 0, 16, tzinfo=timezone.utc),
        dispatch_uploads=lambda: None,
    )

    assert recovered == ["youtube-uploads-automation"]
    assert db.cleared == ["youtube-uploads-automation"]
    assert db.exhausted_projects == ["autotube-expediciones"]


def test_recovery_does_nothing_while_all_projects_within_buffer():
    db = RecoveryDatabase(
        exhausted_projects=["youtube-uploads-automation", "autotube-expediciones"],
        resets={
            "youtube-uploads-automation": "2026-08-15T00:30:00+00:00",
            "autotube-expediciones": "2026-08-15T00:30:00+00:00",
        },
    )

    recovered = main._recover_quota_once(
        db,
        now_utc=datetime(2026, 8, 15, 0, 16, tzinfo=timezone.utc),
        dispatch_uploads=lambda: None,
    )

    assert recovered == []
    assert db.cleared == []
    assert len(db.exhausted_projects) == 2
