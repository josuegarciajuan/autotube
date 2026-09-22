"""C4 — los slots fantasma no deben contar como cobertura de publicación."""

from datetime import datetime

from api.services.recovery_planner import _slot_is_productive

NOW = datetime(2026, 9, 22, 12, 0, 0)


def test_running_slot_is_productive():
    assert _slot_is_productive({"status": "running"}, NOW)


def test_pending_slot_with_video_is_productive():
    assert _slot_is_productive(
        {"status": "pending", "video_id": 5, "scheduled_at": "2026-09-22 08:00:00"},
        NOW,
    )


def test_pending_slot_with_job_is_productive():
    assert _slot_is_productive(
        {"status": "pending", "job_id": 9, "scheduled_at": "2026-09-22 08:00:00"},
        NOW,
    )


def test_past_due_pending_without_video_is_phantom():
    assert not _slot_is_productive(
        {"status": "pending", "scheduled_at": "2026-09-22 08:00:00"}, NOW,
    )


def test_future_pending_without_video_is_productive():
    assert _slot_is_productive(
        {"status": "pending", "scheduled_at": "2026-09-22 18:00:00"}, NOW,
    )
