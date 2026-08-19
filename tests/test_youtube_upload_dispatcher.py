"""Contract tests for the project-aware YouTube upload dispatcher."""

from dataclasses import dataclass

import pytest

from api.services.youtube_upload_dispatcher import (
    UploadDispatchBlocked,
    YouTubeUploadDispatcher,
)


@dataclass
class FakeDatabase:
    reservation: dict | None = None
    finalized: list[tuple[int, bool]] = None

    def __post_init__(self):
        self.finalized = []

    def reserve_youtube_quota(self, **kwargs):
        self.reservation = kwargs
        return {"granted": True, "reservation_id": 42}

    def finalize_youtube_quota_reservation(self, reservation_id, *, consumed):
        self.finalized.append((reservation_id, consumed))


def _dispatcher(db, **kwargs):
    return YouTubeUploadDispatcher(
        channel_slug="canal2",
        db=db,
        project_resolver=lambda _: "project-canal2",
        quota_day=lambda: "2026-08-15",
        automatic_budget=8000,
        **kwargs,
    )


def test_dispatch_reserves_exactly_1600_with_durable_reference_before_transport():
    db = FakeDatabase()
    dispatcher = _dispatcher(db, remediation_mode=False)
    observed = []

    result = dispatcher.dispatch(
        reference_id="video:123",
        content_class="long",
        transport=lambda request_started: observed.append((db.reservation, request_started)) or "ok",
    )

    assert result == "ok"
    assert db.reservation == {
        "project_id": "project-canal2",
        "quota_day_pt": "2026-08-15",
        "operation": "videos.insert",
        "content_class": "long",
        "units": 1600,
        "reference_id": "video:123",
        "automatic_budget": 8000,
    }
    assert observed[0][0] == db.reservation
    assert db.finalized == [(42, False)]


def test_dispatch_consumes_reservation_when_transport_started_then_fails():
    db = FakeDatabase()
    dispatcher = _dispatcher(db, remediation_mode=False)

    def transport(request_started):
        request_started()
        raise TimeoutError("connection lost after request may have reached YouTube")

    with pytest.raises(TimeoutError):
        dispatcher.dispatch("video:123", "long", transport)

    assert db.finalized == [(42, True)]


def test_dispatch_releases_reservation_only_when_transport_fails_before_request():
    db = FakeDatabase()
    dispatcher = _dispatcher(db, remediation_mode=False)

    with pytest.raises(ValueError, match="invalid metadata"):
        dispatcher.dispatch(
            "video:123", "long", lambda request_started: (_ for _ in ()).throw(ValueError("invalid metadata"))
        )

    assert db.finalized == [(42, False)]


def test_remediation_mode_blocks_unapproved_upload_before_reserving_or_transport():
    db = FakeDatabase()
    dispatcher = _dispatcher(db, remediation_mode=True)

    with pytest.raises(UploadDispatchBlocked, match="remediation"):
        dispatcher.dispatch("video:123", "long", lambda request_started: request_started())

    assert db.reservation is None
    assert db.finalized == []


def test_unknown_project_and_missing_reference_fail_closed():
    db = FakeDatabase()
    unknown = YouTubeUploadDispatcher(
        channel_slug="canal2",
        db=db,
        project_resolver=lambda _: "unknown",
        remediation_mode=False,
    )

    with pytest.raises(UploadDispatchBlocked, match="project"):
        unknown.dispatch("video:123", "long", lambda request_started: None)
    with pytest.raises(UploadDispatchBlocked, match="reference"):
        _dispatcher(db, remediation_mode=False).dispatch("", "long", lambda request_started: None)


def test_dispatch_denied_when_reservation_already_consumed_collision():
    """Regression (ago 2026): a reference collision (two videos sharing the
    renamed temp path "video.mp4") must deny admission BEFORE any transport
    request — the caller classifies it as UploadAdmissionDeniedError, NOT
    QuotaExhaustedError, so the per-project quota breaker never trips."""

    class DeniedDb:
        def __init__(self):
            self.reservation = None
            self.finalized = []

        def reserve_youtube_quota(self, **kwargs):
            self.reservation = kwargs
            return {"granted": False, "reason": "already_consumed", "used_units": 1600, "budget_units": 8000}

        def finalize_youtube_quota_reservation(self, reservation_id, *, consumed):
            self.finalized.append((reservation_id, consumed))

    db = DeniedDb()
    transport_called = []

    with pytest.raises(UploadDispatchBlocked, match="already_consumed"):
        _dispatcher(db, remediation_mode=False).dispatch(
            "upload:canal2:/root/autotube/output/videos/video.mp4",
            "long",
            lambda request_started: transport_called.append(request_started) or "ok",
        )

    # No request may cross the boundary, and no reservation is finalized.
    assert transport_called == []
    assert db.finalized == []
