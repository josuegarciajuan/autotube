"""Regression tests for the 24/7 factory recovery guards."""

from datetime import datetime, timedelta, timezone


def test_resume_tick_treats_explicit_none_phase_as_inactive():
    from api.main import _resume_phase_is_active

    assert _resume_phase_is_active({"phase": None}) is False
    assert _resume_phase_is_active({"phase": 1}) is True


def test_invalid_resume_plan_entry_is_safe_and_blocked():
    from api.services.gradual_resume import _phase_for

    today = datetime.now(timezone.utc).date()
    assert _phase_for(None, today) == 0
    assert _phase_for({"start_iso": "not-a-date"}, today) == 0
    assert _phase_for({"start_iso": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, today) == 0


def test_collision_guard_can_ignore_the_slot_being_dispatched(tmp_path):
    import sqlite3
    from pipeline.publish_scheduler import _avoid_channel_collision

    path = tmp_path / "collision.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE videos (id INTEGER PRIMARY KEY, channel_id INTEGER, target_public_at TEXT, titulo_final TEXT);
        CREATE TABLE planned_slots (id INTEGER PRIMARY KEY, channel_id INTEGER, target_public_at TEXT, status TEXT, video_id INTEGER);
        CREATE TABLE video_lifecycle_actions (id INTEGER PRIMARY KEY, channel_id INTEGER, video_id INTEGER, action_type TEXT, status TEXT, scheduled_for TEXT);
    """)
    target = "2026-08-30T12:00:00+00:00"
    conn.execute("INSERT INTO planned_slots VALUES (7, 1, ?, 'running', NULL)", (target,))
    conn.commit()
    conn.close()

    class DB:
        def _connect(self):
            c = sqlite3.connect(path)
            c.row_factory = sqlite3.Row
            return c

        def get_system_state(self, key):
            return None

    proposed = datetime.fromisoformat(target)
    assert _avoid_channel_collision(1, proposed, db=DB(), exclude_slot_id=7) == proposed


def test_collision_guard_caps_pathological_forward_shift(tmp_path):
    import sqlite3
    from pipeline.publish_scheduler import _avoid_channel_collision

    path = tmp_path / "many-collisions.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE videos (id INTEGER PRIMARY KEY, channel_id INTEGER, target_public_at TEXT, titulo_final TEXT);
        CREATE TABLE planned_slots (id INTEGER PRIMARY KEY, channel_id INTEGER, target_public_at TEXT, status TEXT, video_id INTEGER);
        CREATE TABLE video_lifecycle_actions (id INTEGER PRIMARY KEY, channel_id INTEGER, video_id INTEGER, action_type TEXT, status TEXT, scheduled_for TEXT);
    """)
    target = datetime.now(timezone.utc).replace(microsecond=0)
    for index in range(12):
        conn.execute(
            "INSERT INTO videos VALUES (?, 1, ?, 'collision')",
            (index + 1, (target + timedelta(hours=index * 24)).isoformat()),
        )
    conn.commit()
    conn.close()

    class DB:
        def _connect(self):
            c = sqlite3.connect(path)
            c.row_factory = sqlite3.Row
            return c

        def get_system_state(self, key):
            return None

    adjusted = _avoid_channel_collision(1, target, db=DB())
    assert adjusted - target <= timedelta(hours=120)
