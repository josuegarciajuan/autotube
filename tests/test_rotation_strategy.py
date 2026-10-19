"""Contract tests for the circular rotating publication strategy.

The strategy is the planning authority for the daily public target:
base program + the phase bonus (normal / shorts+1 / largos+1 / random+1),
with four channels always holding four distinct phases.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase, migrate_v2
from api.time_utils import MADRID, madrid_day_range

from api.services import rotation_strategy as rs
from api.services.channel_policy import (
    resolve_channel_policy_values,
)
from api.services.planning_service import (
    _publish_cap_per_day,
    _resolve_videos_per_day,
    invalidate_policy_caches,
)


class FakeDB:
    """Minimal DB adapter accepted by the resolvers and the strategy."""

    def __init__(self, *, channels=None, state=None, configs=None):
        self.db_path = "rotation-test.db"
        self.state = dict(state or {})
        self.channels = channels or [
            {"id": 1, "slug": "canal2", "name": "Canal 2"},
            {"id": 2, "slug": "canal3", "name": "Canal 3"},
            {"id": 3, "slug": "canal4", "name": "Canal 4"},
            {"id": 4, "slug": "canal5", "name": "Canal 5"},
        ]
        self.configs = dict(configs or {})

    def get_channels(self, active_only=True):
        return list(self.channels)

    def get_channel(self, channel_id):
        for ch in self.channels:
            if int(ch["id"]) == int(channel_id):
                return dict(ch)
        return None

    def get_channel_planning_config(self, channel_id):
        return dict(self.configs.get(int(channel_id), {}))

    def get_delivery_profile(self, state):
        return {
            "strike": {"public_videos_per_day": 1, "native_shorts_per_day": 1},
            "recovery": {"public_videos_per_day": 1, "native_shorts_per_day": 2},
            "normal": {"public_videos_per_day": 2, "native_shorts_per_day": 3},
        }.get(state)

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value


def _prepare(db, anchor: date, *, enabled=True):
    rs.reset_strategy(db, enabled=enabled)
    strategy = rs.get_strategy(db)
    strategy["anchor_date"] = anchor.isoformat()
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    return rs.get_strategy(db)


def test_initial_assignment_is_a_distinct_phase_permutation():
    db = FakeDB()
    _prepare(db, date(2026, 10, 20))

    phases = [rs.phase_for(cid, db, date(2026, 10, 20))["phase"] for cid in (1, 2, 3, 4)]

    assert len(set(phases)) == 4
    assert set(phases) == set(rs.PHASES)


def test_each_day_keeps_four_distinct_phases_and_cycles_every_four_days():
    anchor = date(2026, 10, 20)
    db = FakeDB()
    _prepare(db, anchor)

    for offset in range(4):
        day = anchor + timedelta(days=offset)
        phases = [rs.phase_for(cid, db, day)["phase"] for cid in (1, 2, 3, 4)]
        assert len(set(phases)) == 4, f"day {day} repeated a phase: {phases}"

    for cid in (1, 2, 3, 4):
        seen = {
            rs.phase_for(cid, db, anchor + timedelta(days=offset))["phase"]
            for offset in range(4)
        }
        assert seen == set(rs.PHASES)


def test_random_plus_one_is_stable_and_balanced_over_a_block():
    anchor = date(2026, 10, 20)
    db = FakeDB()
    _prepare(db, anchor)

    bonus_types = []
    for offset in range(4):
        day = anchor + timedelta(days=offset)
        random_channel = next(
            cid for cid in (1, 2, 3, 4)
            if rs.phase_for(cid, db, day)["phase"] == "random+1"
        )
        first = rs.bonus_for(random_channel, db, day)
        second = rs.bonus_for(random_channel, db, day)
        assert first == second
        bonus_types.append(first["bonus_type"])

    assert bonus_types.count("long") == 2
    assert bonus_types.count("short") == 2


def test_day_assignment_is_persisted_idempotently():
    anchor = date(2026, 10, 20)
    db = FakeDB()
    _prepare(db, anchor)

    first = rs.day_assignment(db, anchor)
    raw_first = db.get_system_state(f"{rs.DAY_KEY_PREFIX}{anchor.isoformat()}")
    second = rs.day_assignment(db, anchor)
    raw_second = db.get_system_state(f"{rs.DAY_KEY_PREFIX}{anchor.isoformat()}")

    assert first == second
    assert raw_first == raw_second
    assert len(first) == 4


def test_long_bonus_adds_exactly_one_public_long_and_upload_slot():
    anchor = date(2026, 10, 20)
    db = FakeDB(
        state={"pacing_profile": "normal"},
        configs={1: {"public_videos_per_day": 2, "planning_enabled": True}},
    )
    strategy = _prepare(db, anchor)
    strategy["initial_phase_index"]["1"] = str(rs.PHASES.index("largos+1"))
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    invalidate_policy_caches()

    policy = resolve_channel_policy_values(1, db=db, day=anchor)
    assert policy["longform_publish_cap"] == 3
    assert policy["public_longform_per_day"] == 3
    assert policy["upload_capacity_per_day"] == 3
    assert policy["rotation_phase"] == "largos+1"
    assert policy["rotation_extra_longs"] == 1
    # Safety gaps are untouched.
    assert policy["same_channel_publish_gap_h"] == 6


def test_short_bonus_adds_exactly_one_native_short():
    anchor = date(2026, 10, 20)
    db = FakeDB(
        state={"pacing_profile": "normal"},
        configs={1: {"public_videos_per_day": 2, "planning_enabled": True}},
    )
    strategy = _prepare(db, anchor)
    strategy["initial_phase_index"]["1"] = str(rs.PHASES.index("shorts+1"))
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    invalidate_policy_caches()

    policy = resolve_channel_policy_values(1, db=db, day=anchor)
    assert policy["longform_publish_cap"] == 2
    assert policy["native_shorts_per_day"] == 4
    assert policy["public_shorts_per_day"] == 4
    assert policy["rotation_phase"] == "shorts+1"
    assert policy["rotation_extra_shorts"] == 1


def test_normal_phase_keeps_program_and_disabled_rotation_is_a_noop():
    anchor = date(2026, 10, 20)
    db = FakeDB(
        state={"pacing_profile": "normal"},
        configs={1: {"public_videos_per_day": 2, "planning_enabled": True}},
    )
    strategy = _prepare(db, anchor)
    strategy["initial_phase_index"]["1"] = str(rs.PHASES.index("normal"))
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    invalidate_policy_caches()

    enabled = resolve_channel_policy_values(1, db=db, day=anchor)
    assert enabled["longform_publish_cap"] == 2
    assert enabled["native_shorts_per_day"] == 3
    assert enabled["rotation_phase"] == "normal"

    rs.set_enabled(db, False)
    invalidate_policy_caches()
    disabled = resolve_channel_policy_values(1, db=db, day=anchor)
    assert disabled["longform_publish_cap"] == 2
    assert disabled["native_shorts_per_day"] == 3
    assert disabled["rotation_phase"] is None
    assert disabled["rotation_extra_longs"] == 0


def test_base_resolver_can_exclude_rotation_for_ramps_and_config_writes():
    anchor = date(2026, 10, 20)
    db = FakeDB(
        state={"pacing_profile": "normal"},
        configs={1: {"public_videos_per_day": 2, "planning_enabled": True}},
    )
    strategy = _prepare(db, anchor)
    strategy["initial_phase_index"]["1"] = str(rs.PHASES.index("shorts+1"))
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    invalidate_policy_caches()

    base = resolve_channel_policy_values(
        1, db=db, day=anchor, include_rotation=False,
    )
    effective = resolve_channel_policy_values(1, db=db, day=anchor)

    assert base["longform_publish_cap"] == 2
    assert base["native_shorts_per_day"] == 3
    assert base["rotation_phase"] is None
    assert effective["native_shorts_per_day"] == 4


def test_non_participant_channel_never_receives_a_bonus():
    anchor = date(2026, 10, 20)
    db = FakeDB(
        state={"pacing_profile": "normal"},
        configs={99: {"public_videos_per_day": 2, "planning_enabled": True}},
    )
    _prepare(db, anchor)

    assert rs.bonus_for(99, db, anchor)["longs"] == 0
    assert rs.bonus_for(99, db, anchor)["shorts"] == 0


def test_planning_target_is_date_aware_and_bypasses_legacy_random_boost():
    anchor = date(2026, 10, 20)
    db = FakeDB(
        state={"pacing_profile": "normal"},
        configs={1: {
            "public_videos_per_day": 2,
            "videos_day_boost_weight": 1.0,
            "planning_enabled": True,
        }},
    )
    strategy = _prepare(db, anchor)
    strategy["initial_phase_index"]["1"] = str(rs.PHASES.index("largos+1"))
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    invalidate_policy_caches()

    cfg = db.get_channel_planning_config(1)
    cfg["channel_id"] = 1
    assert _resolve_videos_per_day(cfg, anchor.isoformat(), db=db) == 3

    # +3 days → shorts+1 for this channel: program only, no long bonus and no
    # legacy random +1 (the rotation branch bypasses it).
    next_day = anchor + timedelta(days=3)
    assert _resolve_videos_per_day(cfg, next_day.isoformat(), db=db) == 2


def test_publish_cap_cache_is_keyed_by_day():
    anchor = date(2026, 10, 20)
    db = FakeDB(
        state={"pacing_profile": "normal"},
        configs={1: {"public_videos_per_day": 2, "planning_enabled": True}},
    )
    strategy = _prepare(db, anchor)
    strategy["initial_phase_index"]["1"] = str(rs.PHASES.index("largos+1"))
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    invalidate_policy_caches()

    cfg = db.get_channel_planning_config(1)
    assert _publish_cap_per_day(db, 1, cfg, day=anchor.isoformat()) == 3
    # +3 days → shorts+1 for this channel: no long bonus on that date.
    assert _publish_cap_per_day(
        db, 1, cfg, day=(anchor + timedelta(days=3)).isoformat()
    ) == 2


def test_rotation_never_reduces_anti_spam_gaps_or_resurrects_zero():
    anchor = date(2026, 10, 20)
    db = FakeDB(
        state={"pacing_profile": "strike"},
        configs={1: {"public_videos_per_day": 2, "planning_enabled": True}},
    )
    strategy = _prepare(db, anchor)
    strategy["initial_phase_index"]["1"] = str(rs.PHASES.index("largos+1"))
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    invalidate_policy_caches()

    policy = resolve_channel_policy_values(1, db=db, day=anchor)
    # Strike base 1 + rotation 1 = 2; gaps remain the strict strike values.
    assert policy["longform_publish_cap"] == 2
    assert policy["same_channel_publish_gap_h"] == 24
    assert policy["same_channel_upload_gap_h"] == 6


def _real_db(tmp_path):
    path = tmp_path / "rotation.db"
    init_db(str(path))
    migrate_v2(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) "
            "VALUES (1, 'Uno', 'canal2', ?, 1)",
            (json.dumps({"public_videos_per_day": 2, "videos_per_day": 2,
                         "planning_enabled": True}),),
        )
        conn.execute(
            "INSERT INTO shorts_planning_config "
            "(channel_id, shorts_native_per_day, shorts_enabled) "
            "VALUES (1, 3, 1)"
        )
        conn.commit()
    return ExtendedDatabase(str(path))


def _set_phase_for_today(db, phase: str):
    today = datetime.now(MADRID).date()
    strategy = rs.reset_strategy(db, enabled=True)
    anchor = date.fromisoformat(strategy["anchor_date"])
    offset = (today - anchor).days
    strategy["initial_phase_index"]["1"] = str(
        (rs.PHASES.index(phase) - offset) % len(rs.PHASES)
    )
    db.set_system_state(rs.STRATEGY_KEY, json.dumps(strategy))
    rs.invalidate_cache()
    invalidate_policy_caches()


def test_cleanup_keeps_the_rotation_extra_and_trims_it_when_phase_rotates_out(tmp_path):
    db = _real_db(tmp_path)
    today = datetime.now(MADRID).date().isoformat()
    start_utc = madrid_day_range(today)[0]

    with db._connect() as conn:
        for idx in range(4):
            conn.execute(
                """INSERT INTO shorts_planned_slots
                   (channel_id, date_key, scheduled_at, short_type, status)
                   VALUES (1, ?, ?, 'native', 'pending')""",
                (today, f"{start_utc[:10]} {10 + idx}:00:00"),
            )
        conn.commit()

    _set_phase_for_today(db, "shorts+1")
    from api.services.shorts_scheduler import cleanup_excessive_shorts_slots
    kept = cleanup_excessive_shorts_slots(db)
    assert kept["cancelled_native"] == 0
    with db._connect() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM shorts_planned_slots "
            "WHERE channel_id=1 AND date_key=? AND status='pending'",
            (today,),
        ).fetchone()[0]
    assert active == 4

    # Al rotar fuera de shorts+1 el objetivo vuelve a 3 y el sobrante se corta.
    _set_phase_for_today(db, "normal")
    trimmed = cleanup_excessive_shorts_slots(db)
    assert trimmed["cancelled_native"] == 1
    with db._connect() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM shorts_planned_slots "
            "WHERE channel_id=1 AND date_key=? AND status='pending'",
            (today,),
        ).fetchone()[0]
    assert active == 3
