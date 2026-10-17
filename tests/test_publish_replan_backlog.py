"""Fase 5: Reprogramar Ahora cubre todo el backlog y valida antes de YouTube."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase, migrate_v2


def _db(tmp_path, name="backlog.db"):
    path = tmp_path / name
    init_db(str(path))
    migrate_v2(str(path))
    db = ExtendedDatabase(str(path))
    with db._connect() as conn:
        conn.execute("DELETE FROM channels")
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) "
            "VALUES (1, 'One', 'one', ?, 1)",
            (json.dumps({"PUBLISH_TIMEZONE": "Europe/Madrid"}),),
        )
        conn.commit()
    return db


def _add_video(db, *, status, video_path, uploaded_at=None,
               scheduled_upload_at=None, yt_video_id=None):
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO videos
               (channel_id, canal, video_path, status, publish_mode,
                uploaded_at, scheduled_upload_at, yt_video_id, created_at)
               VALUES (1, 'one', ?, ?, 'scheduled', ?, ?, ?, datetime('now'))""",
            (video_path, status, uploaded_at, scheduled_upload_at, yt_video_id),
        )
        conn.commit()


@pytest.fixture
def repack_policy(monkeypatch):
    """Política determinista: 1 long/día, gap 24h, warmup máx 48h."""
    import api.services.channel_policy as cp
    values = {
        "longform_publish_cap": 1,
        "same_channel_publish_gap_h": 24,
        "max_warmup_hours": 48,
    }
    monkeypatch.setattr(
        cp, "policy_value",
        lambda cid, key, db=None, default=None: values.get(key, default),
    )
    monkeypatch.setattr(
        cp, "resolve_channel_policy_values",
        lambda cid, db=None, config=None: {
            **values, "publish_target_hour": None,
            "public_longform_per_day": 1,
        },
    )
    import pipeline.publish_scheduler as ps
    monkeypatch.setattr(ps, "_channel_peak_hours", lambda *a, **k: [9])
    return values


def test_repack_covers_every_pending_status(tmp_path, repack_policy):
    from pipeline.publish_scheduler import repack_channel_publish_times

    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_video(db, status="uploaded_private", video_path="/a.mp4",
               uploaded_at=(now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
               yt_video_id="yt-a")
    _add_video(db, status="warming", video_path="/b.mp4",
               uploaded_at=(now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
               yt_video_id="yt-b")
    _add_video(db, status="awaiting_upload", video_path="/c.mp4",
               scheduled_upload_at=(now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
    _add_video(db, status="ready", video_path="/d.mp4",
               scheduled_upload_at=(now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))

    plan = repack_channel_publish_times(db, 1, "one", gap_hours=24)

    assert len(plan) == 4
    assert {p["status"] for p in plan} == {
        "uploaded_private", "warming", "awaiting_upload", "ready",
    }
    # Cap 1/día: ningún día local con más de una publicación.
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Madrid")
    per_day = {}
    for item in plan:
        day = datetime.fromisoformat(item["new_target"]).astimezone(tz).date()
        per_day[day] = per_day.get(day, 0) + 1
    assert max(per_day.values()) <= 1, per_day


def test_repack_schedules_upload_close_to_publication(tmp_path, repack_policy):
    from pipeline.publish_scheduler import repack_channel_publish_times
    from api.time_utils import parse_utc

    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_video(db, status="awaiting_upload", video_path="/a.mp4",
               scheduled_upload_at=(now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))

    plan = repack_channel_publish_times(db, 1, "one", gap_hours=24)
    assert len(plan) == 1
    item = plan[0]
    assert item["adjusted_upload_at"], item

    target = datetime.fromisoformat(item["new_target"])
    upload = parse_utc(item["adjusted_upload_at"])
    # Subida + warmup (120) + buffer (60) = 3h antes de publicar.
    assert timedelta(hours=2) <= (target - upload) <= timedelta(hours=48)


def test_apply_repack_aborts_invalid_plan_before_youtube(tmp_path, monkeypatch):
    """Un plan con gap < policy no debe llamar a YouTube ni escribir en DB."""
    import api.services.publish_repack as pr

    db = _db(tmp_path)
    base = datetime.now(timezone.utc) + timedelta(days=1)
    invalid_plan = [
        {
            "video_id": 1, "status": "uploaded_private", "yt_video_id": "yt-1",
            "old_target": "", "new_target": base.isoformat(),
            "requires_yt_update": True, "adjusted_upload_at": None,
            "uploaded_at": (base - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "scheduled_upload_at": None, "is_marathon": False, "preserved": False,
        },
        {
            "video_id": 2, "status": "warming", "yt_video_id": "yt-2",
            "old_target": "", "new_target": (base + timedelta(minutes=10)).isoformat(),
            "requires_yt_update": True, "adjusted_upload_at": None,
            "uploaded_at": (base - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "scheduled_upload_at": None, "is_marathon": False, "preserved": False,
        },
    ]
    monkeypatch.setattr(
        "pipeline.publish_scheduler.repack_channel_publish_times",
        lambda *a, **k: invalid_plan,
    )
    monkeypatch.setattr(
        pr, "_policy",
        lambda db, cid: {
            "same_channel_publish_gap_h": 24,
            "longform_publish_cap": 1,
            "max_warmup_hours": 48,
        },
    )

    called = {"yt": False}

    class _Uploader:
        def __init__(self, *a, **k):
            called["yt"] = True

    monkeypatch.setattr("pipeline.youtube_uploader.YouTubeUploader", _Uploader)

    result = pr.apply_publish_repack(
        db, 1, "one", dry_run=False, quota_gate=False, force_yt=True,
    )

    assert result["plan_valid"] is False
    assert result["rescheduled"] == 0
    assert result["validation_errors"]
    assert called["yt"] is False
