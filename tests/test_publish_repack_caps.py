"""Regression tests for daily long-form cap accounting in repack."""

import datetime as dt
import json
from collections import Counter
from datetime import datetime, timezone

import pytz

from database.db import init_db
from database.db_extended import ExtendedDatabase, migrate_v2


def _db(tmp_path):
    path = tmp_path / "repack-caps.db"
    init_db(str(path))
    migrate_v2(str(path))
    with ExtendedDatabase(str(path))._connect() as conn:
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) VALUES (1, 'One', 'one', ?, 1)",
            (json.dumps({"PUBLISH_TIMEZONE": "Europe/Madrid"}),),
        )
        conn.commit()
    return ExtendedDatabase(str(path))


def test_published_normal_longform_consumes_repack_day_budget(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO videos
                (channel_id, canal, video_path, status, publish_mode, published_at, is_marathon)
                VALUES (1, 'one', '/tmp/one.mp4', 'published', 'scheduled', ?, 0)""",
            (now,),
        )
        conn.commit()

    from pipeline.publish_scheduler import _published_normal_counts_by_local_day

    counts = _published_normal_counts_by_local_day(db, 1, "Europe/Madrid")
    assert sum(counts.values()) == 1


def test_published_marathon_does_not_consume_repack_day_budget(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO videos
                (channel_id, canal, video_path, status, publish_mode, published_at, is_marathon)
                VALUES (1, 'one', '/tmp/one.mp4', 'published', 'scheduled', ?, 1)""",
            (now,),
        )
        conn.commit()

    from pipeline.publish_scheduler import _published_normal_counts_by_local_day

    assert _published_normal_counts_by_local_day(db, 1, "Europe/Madrid") == {}


def test_channel_peak_hours_keeps_midnight_slot():
    """Regression: target_hour=0 must not be dropped as falsy."""
    from pipeline.publish_scheduler import _channel_peak_hours

    class DB:
        def get_optimal_slots(self, cid, content_type):
            return [
                {"slot_rank": 1, "target_hour": 0},
                {"slot_rank": 2, "target_hour": 11},
                {"slot_rank": 3, "target_hour": 7},
            ]

    hours = _channel_peak_hours(DB(), 1, {}, 21)
    assert 0 in hours


def test_dense_backlog_is_spread_not_stacked_at_safety_bound(tmp_path, monkeypatch):
    """A 9-video queue at 2/day must span days, never pile on the safety bound."""
    db = _db(tmp_path)
    with db._connect() as conn:
        for i in range(9):
            conn.execute(
                """INSERT INTO videos
                    (channel_id, canal, video_path, status, publish_mode, created_at)
                    VALUES (1, 'one', ?, 'awaiting_upload', 'scheduled', ?)""",
                (f"/tmp/v{i}.mp4", f"2026-09-01 00:00:{i:02d}"),
            )
        conn.commit()

    import api.services.channel_policy as channel_policy
    monkeypatch.setattr(
        channel_policy, "policy_value",
        lambda cid, key, db=None, default=None: {
            "longform_publish_cap": 2, "same_channel_publish_gap_h": 6,
        }.get(key, default),
    )
    import pipeline.publish_scheduler as scheduler
    monkeypatch.setattr(scheduler, "_channel_peak_hours",
                        lambda *a, **k: [9, 15, 21])

    plan = scheduler.repack_channel_publish_times(db, 1, "one", gap_hours=6)
    assert len(plan) == 9

    tz = pytz.timezone("Europe/Madrid")
    per_day: Counter = Counter()
    targets = []
    for item in plan:
        when = dt.datetime.fromisoformat(item["new_target"]).astimezone(tz)
        per_day[when.date()] += 1
        targets.append(item["new_target"])

    assert max(per_day.values()) <= 2, per_day
    assert len(set(targets)) == 9, targets
