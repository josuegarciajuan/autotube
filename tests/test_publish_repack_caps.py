"""Regression tests for daily long-form cap accounting in repack."""

import json
from datetime import datetime, timezone

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
