"""Remote YouTube update controls for the authoritative replan.

Covers the phase-4 invariants:
  - videos.update has a bounded retry budget for transient errors only;
  - the authoritative repack enforces a total item budget;
  - an item skipped by that budget is NOT written to the DB, so DB and YouTube
    never disagree about the public target.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from googleapiclient.errors import HttpError

import api.services.publish_coverage as pc
import api.services.publish_repack as pr
import api.services.planning_service as ps
from database.db import init_db
from database.db_extended import ExtendedDatabase, migrate_v2


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"
        self.headers = {}


def _http_error(reason, status):
    body = json.dumps({
        "error": {
            "code": status,
            "message": reason,
            "errors": [{"message": reason, "domain": "youtube", "reason": reason}],
        }
    }).encode()
    return HttpError(_Resp(status), body)


def _uploader():
    from pipeline.youtube_uploader import YouTubeUploader
    up = object.__new__(YouTubeUploader)
    up.channel_slug = "canal2"
    up.account_name = ""
    up.db = None
    up._credentials = None
    up._update_http = None
    return up


def test_status_update_retries_transient_http_error(monkeypatch):
    import pipeline.youtube_uploader as yu

    monkeypatch.setattr(yu, "STATUS_UPDATE_RETRIES", 2)
    monkeypatch.setattr(yu, "STATUS_UPDATE_RETRY_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(yu.time, "sleep", lambda _s: None)

    up = _uploader()
    monkeypatch.setattr(up, "_raise_if_quota_exceeded", lambda *a, **k: None)
    calls = {"n": 0}

    class _Request:
        def execute(self, http=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error("backendError", 503)
            return {}

    attempts = up._execute_status_update(
        lambda: _Request(), video_id="yt-1",
    )

    assert attempts == 3
    assert calls["n"] == 3


def test_status_update_does_not_retry_permanent_error(monkeypatch):
    import pipeline.youtube_uploader as yu

    monkeypatch.setattr(yu, "STATUS_UPDATE_RETRIES", 3)
    monkeypatch.setattr(yu.time, "sleep", lambda _s: None)
    up = _uploader()
    monkeypatch.setattr(up, "_raise_if_quota_exceeded", lambda *a, **k: None)
    calls = {"n": 0}

    class _Request:
        def execute(self, http=None):
            calls["n"] += 1
            raise _http_error("invalidRequest", 400)

    with pytest.raises(HttpError):
        up._execute_status_update(lambda: _Request(), video_id="yt-1")

    assert calls["n"] == 1


def test_status_update_retries_network_timeout(monkeypatch):
    import pipeline.youtube_uploader as yu

    monkeypatch.setattr(yu, "STATUS_UPDATE_RETRIES", 1)
    monkeypatch.setattr(yu, "STATUS_UPDATE_RETRY_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(yu.time, "sleep", lambda _s: None)
    up = _uploader()
    calls = {"n": 0}

    class _Request:
        def execute(self, http=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("socket timed out")
            return {}

    attempts = up._execute_status_update(lambda: _Request(), video_id="yt-1")

    assert attempts == 2
    assert calls["n"] == 2


def _db(tmp_path):
    path = tmp_path / "remote-budget.db"
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
        now = datetime.now(timezone.utc)
        rows = [
            (1, "yt-1", (now + timedelta(days=1)).isoformat()),
            (2, "yt-2", (now + timedelta(days=2)).isoformat()),
            (3, "yt-3", (now + timedelta(days=3)).isoformat()),
        ]
        for vid, yt_id, target in rows:
            conn.execute(
                """INSERT INTO videos
                   (id, channel_id, canal, video_path, status, publish_mode,
                    yt_video_id, target_public_at, uploaded_at, created_at)
                   VALUES (?, 1, 'one', ?, 'warming', 'scheduled', ?, ?, ?, datetime('now'))""",
                (vid, f"/v{vid}.mp4", yt_id, target,
                 (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")),
            )
        conn.commit()
    return db, rows


def test_repack_honours_youtube_update_budget(tmp_path, monkeypatch):
    db, rows = _db(tmp_path)
    now = datetime.now(timezone.utc)
    plan = []
    for (vid, yt_id, old_target), day in zip(rows, (2, 3, 4)):
        plan.append({
            "video_id": vid,
            "status": "warming",
            "yt_video_id": yt_id,
            "old_target": old_target,
            "new_target": (now + timedelta(days=day)).isoformat(),
            "requires_yt_update": True,
            "adjusted_upload_at": None,
            "uploaded_at": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "scheduled_upload_at": None,
            "is_marathon": False,
            "preserved": False,
        })

    monkeypatch.setattr(
        "pipeline.publish_scheduler.repack_channel_publish_times",
        lambda *a, **k: plan,
    )
    monkeypatch.setattr(pr, "_validate_repack_plan", lambda *a, **k: [])
    monkeypatch.setattr(
        pr, "_policy",
        lambda db, cid: {
            "same_channel_publish_gap_h": 1,
            "longform_publish_cap": 10,
            "max_warmup_hours": 48,
        },
    )

    calls = []

    class _Uploader:
        def __init__(self, *a, **k):
            pass

        def authenticate(self):
            return True

        def set_publish_at(self, yt_id, target):
            calls.append((yt_id, target))
            return {"updated": True}

    monkeypatch.setattr("pipeline.youtube_uploader.YouTubeUploader", _Uploader)

    result = pr.apply_publish_repack(
        db, 1, "one", dry_run=False, quota_gate=False,
        force_yt=True, max_yt_updates=2,
    )

    assert result["yt_attempted"] == 2
    assert result["yt_succeeded"] == 2
    assert result["yt_failed"] == 0
    assert result["yt_skipped_limit"] == 1
    assert len(calls) == 2

    with db._connect() as conn:
        targets = [
            conn.execute("SELECT target_public_at FROM videos WHERE id=?", (vid,)).fetchone()[0]
            for vid in (1, 2, 3)
        ]
    # The two applied items move; the limited item stays exactly in sync with
    # YouTube's untouched target.
    assert targets[0] == plan[0]["new_target"]
    assert targets[1] == plan[1]["new_target"]
    assert targets[2] == rows[2][2]


def _single_plan(rows, now):
    vid, yt_id, old_target = rows[0]
    return [{
        "video_id": vid,
        "status": "warming",
        "yt_video_id": yt_id,
        "old_target": old_target,
        "new_target": (now + timedelta(days=2)).isoformat(),
        "requires_yt_update": True,
        "adjusted_upload_at": None,
        "uploaded_at": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "scheduled_upload_at": None,
        "is_marathon": False,
        "preserved": False,
    }]


def test_repack_rolls_back_youtube_when_db_write_fails(tmp_path, monkeypatch):
    db, rows = _db(tmp_path)
    now = datetime.now(timezone.utc)
    plan = _single_plan(rows, now)
    monkeypatch.setattr(
        "pipeline.publish_scheduler.repack_channel_publish_times",
        lambda *a, **k: plan,
    )
    monkeypatch.setattr(pr, "_validate_repack_plan", lambda *a, **k: [])
    monkeypatch.setattr(
        pr, "_policy",
        lambda db, cid: {
            "same_channel_publish_gap_h": 1,
            "longform_publish_cap": 10,
            "max_warmup_hours": 48,
        },
    )

    calls = []

    class _Uploader:
        def __init__(self, *a, **k):
            pass

        def authenticate(self):
            return True

        def set_publish_at(self, yt_id, target):
            calls.append((yt_id, target))
            return {"updated": True}

    monkeypatch.setattr("pipeline.youtube_uploader.YouTubeUploader", _Uploader)

    original_connect = db._connect

    @contextmanager
    def flaky_connect():
        with original_connect() as conn:
            class _Proxy:
                def execute(self, sql, params=()):
                    if sql.lstrip().upper().startswith(
                        "UPDATE VIDEOS SET TARGET_PUBLIC_AT"
                    ):
                        raise sqlite3.OperationalError("disk I/O error")
                    return conn.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(conn, name)

            yield _Proxy()

    monkeypatch.setattr(db, "_connect", flaky_connect)

    result = pr.apply_publish_repack(
        db, 1, "one", dry_run=False, quota_gate=False,
        force_yt=True, max_yt_updates=10,
    )

    assert result["yt_attempted"] == 1
    assert result["yt_succeeded"] == 1
    assert result["db_failed"] == 1
    assert result["yt_rollback"] == 1
    assert result["yt_db_mismatch"] == 0
    assert result["rescheduled"] == 0
    # Forward update + rollback to the DB's old target.
    assert [call[1] for call in calls] == [
        plan[0]["new_target"], plan[0]["old_target"],
    ]


class _CoverageConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return self

    def fetchall(self):
        return self._rows

    def commit(self):
        pass


class _CoverageDB:
    def __init__(self, rows):
        self._rows = rows

    def _connect(self):
        return _CoverageConn(self._rows)


def test_coverage_honours_remaining_youtube_budget(monkeypatch):
    rows = [
        {"id": i, "status": "warming", "yt_video_id": f"yt-{i}",
         "target_public_at": None, "published_at": None,
         "uploaded_at": "2026-10-26 08:00:00", "created_at": "2026-10-26 08:00:00"}
        for i in (1, 2, 3)
    ]
    calls = []

    class _Uploader:
        def __init__(self, *a, **k):
            pass

        def authenticate(self):
            return True

        def set_publish_at(self, yt_id, target):
            calls.append(yt_id)
            return {"updated": True}

    monkeypatch.setattr("pipeline.youtube_uploader.YouTubeUploader", _Uploader)

    from zoneinfo import ZoneInfo
    out = pc.remediate_today_public_deficit(
        _CoverageDB(rows), "one", 1, ZoneInfo("Europe/Madrid"), 3,
        max_yt_updates=2,
    )

    assert out["yt_attempted"] == 2
    assert out["forced"] == 2
    assert out["yt_failed"] == 0
    assert out["yt_limited"] == 1
    assert calls == ["yt-1", "yt-2"]


def test_authoritative_replan_shares_budget_with_coverage(tmp_path, monkeypatch):
    db, _rows = _db(tmp_path)
    monkeypatch.setenv("AUTHORITATIVE_REPLAN_MAX_YT_UPDATES", "3")
    monkeypatch.setattr(
        "api.services.channel_policy.policy_value",
        lambda *a, **k: 2,
    )

    repack_calls = []
    coverage_calls = []

    def fake_repack(db_arg, channel_id, slug, **kwargs):
        repack_calls.append(kwargs.get("max_yt_updates"))
        return {
            "rescheduled": 2, "yt_attempted": 2, "yt_succeeded": 2,
            "yt_failed": 0, "yt_skipped_limit": 0,
        }

    def fake_coverage(db_arg, slug, channel_id, tz, cap, **kwargs):
        coverage_calls.append(kwargs.get("max_yt_updates"))
        return {
            "forced": 1, "need": 1, "yt_attempted": 1,
            "yt_failed": 0, "yt_limited": 0, "reason": "ok",
        }

    monkeypatch.setattr(pr, "apply_publish_repack", fake_repack)
    monkeypatch.setattr(pc, "remediate_today_public_deficit", fake_coverage)
    monkeypatch.setattr(ps, "compute_and_store_horizon", lambda **k: {})
    monkeypatch.setattr(ps, "request_planning_replan", lambda *a, **k: None)
    monkeypatch.setattr(
        "api.services.shorts_scheduler.generate_upcoming_shorts",
        lambda **k: {},
    )

    result = ps.authoritative_replan(db=db, horizon_days=7)

    assert result["yt_update_budget"] == 3
    assert result["yt_updates_attempted"] == 3
    assert result["yt_updates_succeeded"] == 3
    assert result["yt_updates_failed"] == 0
    assert result["yt_updates_limited"] == 0
    assert repack_calls == [3], "el repack recibe todo el presupuesto inicial"
    assert coverage_calls == [1], "coverage recibe solo el presupuesto restante"
