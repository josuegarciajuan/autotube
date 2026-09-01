import json
import sqlite3

from database.db import init_db
from database.db_extended import ExtendedDatabase, migrate_v2
from api.services import channel_policy


def _db(tmp_path):
    path = tmp_path / "delivery.db"
    init_db(str(path))
    migrate_v2(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) VALUES (1, 'One', 'one', ?, 1)",
            (json.dumps({"videos_per_day": 1, "planning_enabled": True}),),
        )
        conn.commit()
    return ExtendedDatabase(str(path))


def test_channel_state_change_resets_manual_delivery_override(tmp_path):
    db = _db(tmp_path)
    channel_policy.set_channel_delivery_state("recovery", 1, db=db)
    channel_policy.set_channel_delivery_override(1, {"longs_per_day": 0}, db=db)

    assert channel_policy.resolve_channel_policy_values(1, db=db)["longform_publish_cap"] == 0

    channel_policy.set_channel_delivery_state("normal", 1, db=db)

    assert db.get_system_state("channel_delivery_override_1") in (None, "")
    assert channel_policy.get_channel_delivery_state(1, db=db) == "normal"
    assert channel_policy.resolve_channel_policy_values(1, db=db)["longform_publish_cap"] == 2


def test_planning_config_exposes_public_generation_and_upload_capacity(tmp_path):
    db = _db(tmp_path)
    db.update_channel_planning_config(
        1, public_videos_per_day=1, longform_generation_per_day=4,
        upload_capacity_per_day=2,
    )

    config = db.get_channel_planning_config(1)
    assert config["public_videos_per_day"] == 1
    assert config["longform_generation_per_day"] == 4
    assert config["upload_capacity_per_day"] == 2


def test_claim_planned_slot_is_single_winner(tmp_path):
    db = _db(tmp_path)
    slot_id = db.create_planned_slot(1, "2030-01-01", "2030-01-01 10:00:00")

    first = db.claim_planned_slot(slot_id, "worker-a")
    second = db.claim_planned_slot(slot_id, "worker-b")

    assert first is True
    assert second is False
    slot = db.get_planned_slots(date_key="2030-01-01")[0]
    assert slot["status"] == "running"
    assert slot["claimed_by"] == "worker-a"


def test_claim_upload_video_is_single_winner(tmp_path):
    db = _db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO videos (channel_id, canal, status, video_path) VALUES (1, 'one', 'awaiting_upload', '/tmp/video.mp4')"
        )
        video_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

    assert db.claim_upload_video(video_id, "worker-a") is True
    assert db.claim_upload_video(video_id, "worker-b") is False
    video = db.get_video(video_id)
    assert video["status"] == "uploading"
    assert video["upload_claimed_by"] == "worker-a"
