"""Contract tests for the non-destructive full-replan flow."""

import hashlib
import json
import sqlite3
from datetime import date

import pytest

import api.services.planning_service as planning_service
from api.services.planning_service import safe_full_replan_apply, safe_full_replan_preflight
from database.db import init_db
from database.db_extended import ExtendedDatabase, _migrate_v39, migrate_v2


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "safe-replan.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT NOT NULL,
            config_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE generation_jobs (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            video_id INTEGER,
            status TEXT NOT NULL DEFAULT 'queued'
        );
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            status TEXT NOT NULL DEFAULT 'draft'
        );
        CREATE TABLE planned_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            date_key TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            target_upload_at TEXT,
            target_public_at TEXT,
            upload_window_start INTEGER,
            upload_window_end INTEGER,
            slot_position INTEGER DEFAULT 0,
            source_mode TEXT DEFAULT 'original',
            status TEXT NOT NULL DEFAULT 'pending',
            job_id INTEGER
        );
        CREATE TABLE shorts_planned_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            date_key TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            target_upload_at TEXT,
            short_type TEXT NOT NULL DEFAULT 'native',
            long_slot_position INTEGER,
            source_video_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            job_id INTEGER,
            short_id INTEGER,
            slot_position INTEGER DEFAULT 0
        );
        CREATE TABLE shorts_planning_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL UNIQUE,
            shorts_native_per_day INTEGER DEFAULT 3,
            shorts_clip_per_day INTEGER DEFAULT 0,
            shorts_clips_per_long INTEGER DEFAULT 0,
            shorts_enabled INTEGER DEFAULT 1
        );
        """
    )
    conn.execute(
        "INSERT INTO channels (id, name, slug, config_json, active) VALUES (1, 'Channel', 'channel', ?, 1)",
        (json.dumps({"videos_per_day": 1, "planning_enabled": True}),),
    )
    conn.execute("INSERT INTO shorts_planning_config (channel_id) VALUES (1)")
    conn.execute("INSERT INTO generation_jobs (id, channel_id, status) VALUES (41, 1, 'queued')")
    conn.execute(
        """INSERT INTO planned_slots
           (channel_id, date_key, scheduled_at, target_upload_at, status, job_id)
           VALUES (1, ?, '2030-01-01 08:00:00', '2030-01-01 10:00:00', 'pending', 41)""",
        (date.today().isoformat(),),
    )
    _migrate_v39(conn, __import__("logging").getLogger(__name__))
    conn.commit()
    conn.close()

    database = ExtendedDatabase()
    database._db_path = str(path)

    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    database._connect = connect
    return database


def _rows(db):
    with db._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM planned_slots ORDER BY id")]


def _short_rows(db):
    with db._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM shorts_planned_slots ORDER BY id")]


def test_preflight_is_read_only_and_persists_an_opaque_confirmation(db):
    before = _rows(db)

    result = safe_full_replan_preflight(db=db, horizon_days=1)

    assert result["confirmation_token"]
    assert result["proposed_slots"]
    assert _rows(db) == before
    with db._connect() as conn:
        stored = conn.execute("SELECT token_hash FROM safe_replan_confirmations").fetchone()
    assert stored is not None
    assert stored["token_hash"] != result["confirmation_token"]


def test_apply_updates_pending_slots_in_place_without_cancelling_jobs(db):
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    original = _rows(db)[0]

    result = safe_full_replan_apply(preflight["confirmation_token"], db=db)

    updated = _rows(db)[0]
    assert result["ok"] is True
    assert result["updated"] == 1
    assert updated["id"] == original["id"]
    assert updated["job_id"] == 41
    with db._connect() as conn:
        assert conn.execute("SELECT status FROM generation_jobs WHERE id = 41").fetchone()["status"] == "queued"


def test_apply_reads_snapshot_on_the_transaction_connection(db, monkeypatch):
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    original_connect = db._connect
    connections = []

    def tracked_connect():
        conn = original_connect()
        connections.append(conn)
        return conn

    monkeypatch.setattr(db, "_connect", tracked_connect)
    monkeypatch.setattr(db, "set_system_state", lambda *args, **kwargs: None)

    safe_full_replan_apply(preflight["confirmation_token"], db=db)

    assert len(connections) == 1


def test_apply_retries_transient_sqlite_lock(db, monkeypatch):
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    original_snapshot = planning_service._safe_replan_snapshot
    attempts = 0

    def flaky_snapshot(database, conn=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_snapshot(database, conn=conn)

    monkeypatch.setattr(planning_service, "_safe_replan_snapshot", flaky_snapshot)
    monkeypatch.setattr(db, "set_system_state", lambda *args, **kwargs: None)

    result = safe_full_replan_apply(preflight["confirmation_token"], db=db)

    assert result["ok"] is True
    assert attempts == 2


def test_apply_rejects_stale_and_reused_confirmation_tokens(db):
    stale = safe_full_replan_preflight(db=db, horizon_days=1)
    with db._connect() as conn:
        conn.execute("UPDATE planned_slots SET scheduled_at = '2030-01-02 08:00:00' WHERE id = 1")
        conn.commit()

    with pytest.raises(ValueError, match="stale"):
        safe_full_replan_apply(stale["confirmation_token"], db=db)

    fresh = safe_full_replan_preflight(db=db, horizon_days=1)
    safe_full_replan_apply(fresh["confirmation_token"], db=db)
    with pytest.raises(ValueError, match="used"):
        safe_full_replan_apply(fresh["confirmation_token"], db=db)


def test_preflight_reviews_every_pending_backlog_slot_and_reports_actual_counts(db):
    """Pending slots outside the generated horizon are retained explicitly."""
    with db._connect() as conn:
        for day in range(2, 10):
            conn.execute("""
                INSERT INTO planned_slots
                    (channel_id, date_key, scheduled_at, target_upload_at, status)
                VALUES (1, ?, ?, ?, 'pending')
            """, (
                f"2040-01-{day:02d}",
                f"2040-01-{day:02d} 08:00:00",
                f"2040-01-{day:02d} 10:00:00",
            ))
        conn.commit()

    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    review = preflight["review"]
    reviewed_ids = {item["slot_id"] for item in review if item["slot_id"] is not None}

    assert reviewed_ids == set(range(1, 10))
    assert preflight["counts"]["retained"] + preflight["counts"]["rescheduled"] == 9
    assert preflight["counts"]["retained"] > 0
    assert preflight["counts"]["new"] == 0

    applied = safe_full_replan_apply(preflight["confirmation_token"], db=db)

    assert applied["counts"] == preflight["counts"]
    assert {row["id"] for row in _rows(db)} == set(range(1, 10))
    assert applied["review"] == review


def test_active_job_or_video_change_makes_confirmation_stale(db):
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    with db._connect() as conn:
        conn.execute("INSERT INTO videos (id, channel_id, status) VALUES (9, 1, 'generating')")
        conn.execute("INSERT INTO generation_jobs (id, channel_id, video_id, status) VALUES (42, 1, 9, 'running')")
        conn.commit()

    with pytest.raises(ValueError, match="stale"):
        safe_full_replan_apply(preflight["confirmation_token"], db=db)


def test_safe_replan_retimes_pending_shorts_before_creating_new_shorts(db):
    """Pending Shorts keep identity; running Shorts remain wholly untouched."""
    today = date.today().isoformat()
    with db._connect() as conn:
        conn.execute("""
            INSERT INTO shorts_planned_slots
                (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                 long_slot_position, source_video_id, status, job_id, short_id, slot_position)
            VALUES (1, ?, '2030-01-01 08:00:00', '2030-01-01 08:15:00', 'clip',
                    7, 22, 'pending', 51, 61, 3)
        """, (today,))
        conn.execute("""
            INSERT INTO shorts_planned_slots
                (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                 status, job_id, short_id, slot_position)
            VALUES (1, ?, '2030-01-01 09:00:00', '2030-01-01 09:15:00', 'native',
                    'running', 52, 62, 4)
        """, (today,))
        conn.commit()

    original_pending, original_running = _short_rows(db)
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)

    assert preflight["covers"] == "long-form + Shorts"
    assert preflight["counts"]["shorts"]["retained"] + preflight["counts"]["shorts"]["rescheduled"] == 1
    assert preflight["summary"]["shorts"]["proposed"] >= 1

    applied = safe_full_replan_apply(preflight["confirmation_token"], db=db)

    pending, running = _short_rows(db)
    assert pending["id"] == original_pending["id"]
    assert pending["job_id"] == original_pending["job_id"]
    assert pending["short_id"] == original_pending["short_id"]
    assert pending["source_video_id"] == original_pending["source_video_id"]
    assert pending["long_slot_position"] == original_pending["long_slot_position"]
    assert running == original_running
    assert applied["counts"]["shorts"] == preflight["counts"]["shorts"]


def test_pending_or_running_shorts_change_makes_confirmation_stale(db):
    today = date.today().isoformat()
    with db._connect() as conn:
        conn.execute("""
            INSERT INTO shorts_planned_slots
                (channel_id, date_key, scheduled_at, short_type, status)
            VALUES (1, ?, '2030-01-01 08:00:00', 'native', 'pending')
        """, (today,))
        conn.commit()

    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    with db._connect() as conn:
        conn.execute("UPDATE shorts_planned_slots SET status = 'running' WHERE id = 1")
        conn.commit()

    with pytest.raises(ValueError, match="stale"):
        safe_full_replan_apply(preflight["confirmation_token"], db=db)


def test_legacy_full_replan_endpoint_is_gone():
    from api.routers.planning import full_replan
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        full_replan()

    assert exc_info.value.status_code == 410


def test_replan_router_returns_structured_503_for_database_busy(monkeypatch):
    from fastapi import HTTPException
    from api.routers import planning
    from api.services.planning_service import SafeReplanBusyError

    monkeypatch.setattr(planning, "get_db", lambda: object())
    monkeypatch.setattr(
        planning_service,
        "safe_full_replan_apply",
        lambda *args, **kwargs: (_ for _ in ()).throw(SafeReplanBusyError()),
    )

    with pytest.raises(HTTPException) as exc_info:
        planning.safe_full_replan_apply(planning.SafeFullReplanApply(confirmation_token="token"))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "SERVER_BUSY",
        "message": "La base de datos está ocupada; inténtalo de nuevo en unos segundos.",
    }
    assert exc_info.value.headers == {"Retry-After": "5"}


# ═════════════════════════════════════════════════════════════════════════
#  Regression: target_public_at collisions against active planned_slots
#  (uq_active_planned_public_target partial unique index)
# ═════════════════════════════════════════════════════════════════════════

_RUNNING_SLOT = """INSERT INTO planned_slots
    (channel_id, date_key, scheduled_at, target_upload_at, target_public_at,
     upload_window_start, upload_window_end, slot_position, source_mode, status)
    VALUES (?, ?, ?, ?, ?, 9, 11, 0, 'original', 'running')"""
_PENDING_SLOT = """INSERT INTO planned_slots
    (channel_id, date_key, scheduled_at, target_upload_at, target_public_at,
     upload_window_start, upload_window_end, slot_position, source_mode, status)
    VALUES (?, ?, ?, ?, ?, 9, 11, 0, 'original', 'pending')"""


def _proposal(channel_id, date_key, scheduled_at, target_public_at, target_upload_at=None):
    return {
        "channel_id": channel_id,
        "date_key": date_key,
        "scheduled_at": scheduled_at,
        "target_upload_at": target_upload_at if target_upload_at is not None else scheduled_at,
        "target_public_at": target_public_at,
        "upload_window_start": 9,
        "upload_window_end": 11,
        "slot_position": 0,
        "source_mode": "original",
    }


@pytest.fixture
def full_db(tmp_path):
    """Real migrated schema (partial unique index included) plus channel 10."""
    path = tmp_path / "full-replan.db"
    init_db(str(path))
    migrate_v2(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) VALUES (10, 'Fix', 'fix', ?, 1)",
            (json.dumps({"videos_per_day": 1, "planning_enabled": True}),),
        )
        conn.commit()
    return ExtendedDatabase(str(path))


def _preflight_and_apply(monkeypatch, db, long_proposals, shorts_proposals=None):
    """Run preflight + apply with fully controlled proposals (no scheduler)."""
    monkeypatch.setattr(
        planning_service, "_safe_replan_proposed_shorts",
        lambda d, h: shorts_proposals if shorts_proposals is not None else [],
    )
    monkeypatch.setattr(
        planning_service, "_safe_replan_proposed_slots",
        lambda d, h: long_proposals,
    )
    preflight = planning_service.safe_full_replan_preflight(db=db, horizon_days=7)
    applied = planning_service.safe_full_replan_apply(preflight["confirmation_token"], db=db)
    return preflight, applied


def test_long_reschedule_onto_running_target_is_degraded_to_retained(full_db, monkeypatch):
    """(a) A pending slot must never be retimed onto a running slot's target.

    Regression: pre-fix, the plan proposed moving the pending slot to the
    running slot's target_public_at and apply died with sqlite3.IntegrityError
    on the partial unique index. The resolver keeps the pending slot in place
    with reason=collision_active_slot.
    """
    running_target = "2030-01-01T11:00:00+00:00"
    old_target = "2030-01-02T11:00:00+00:00"
    with full_db._connect() as conn:
        conn.execute(_RUNNING_SLOT, (10, "2030-01-01", "2030-01-01 08:00:00",
                                     "2030-01-01 10:00:00", running_target))
        conn.execute(_PENDING_SLOT, (10, "2030-01-02", "2030-01-02 08:00:00",
                                     "2030-01-02 10:00:00", old_target))
        conn.commit()

    preflight, applied = _preflight_and_apply(monkeypatch, full_db, [
        _proposal(10, "2030-01-02", "2030-01-02 08:00:00", running_target),
    ])

    pending_item = next(it for it in preflight["review"] if it["slot_id"] is not None
                        and it["channel_id"] == 10 and it["action"] != "protected")
    assert pending_item["action"] == "retained"
    assert pending_item["reason"] == "collision_active_slot"
    assert preflight["counts"]["rescheduled"] == 0
    assert preflight["counts"]["retained"] == 1

    assert applied["ok"] is True
    with full_db._connect() as conn:
        rows = {row["status"]: row for row in conn.execute(
            "SELECT id, status, target_public_at FROM planned_slots WHERE channel_id=10")}
    assert rows["running"]["target_public_at"] == running_target
    assert rows["pending"]["target_public_at"] == old_target


def test_two_pending_slots_assigned_same_target_second_stays_put(full_db, monkeypatch):
    """(b) Two pending slots whose plan gives both the same after target.

    The first (in apply order) takes the target; the second degrades to
    retained instead of colliding during apply.
    """
    shared_target = "2030-01-03T11:00:00+00:00"
    with full_db._connect() as conn:
        conn.execute(_PENDING_SLOT, (10, "2030-01-01", "2030-01-01 08:00:00",
                                     "2030-01-01 10:00:00", "2030-01-01T11:00:00+00:00"))
        conn.execute(_PENDING_SLOT, (10, "2030-01-02", "2030-01-02 08:00:00",
                                     "2030-01-02 10:00:00", "2030-01-02T11:00:00+00:00"))
        conn.commit()

    preflight, applied = _preflight_and_apply(monkeypatch, full_db, [
        _proposal(10, "2030-01-01", "2030-01-01 08:00:00", shared_target),
        _proposal(10, "2030-01-02", "2030-01-02 08:00:00", shared_target),
    ])

    long_items = [it for it in preflight["review"] if it["kind"] == "long_form"]
    actions = [it["action"] for it in long_items]
    assert actions.count("rescheduled") == 1
    assert actions.count("retained") == 1
    degraded = next(it for it in long_items if it["action"] == "retained")
    assert degraded["reason"] == "collision_active_slot"
    assert degraded["after"] == degraded["before"]

    assert applied["ok"] is True
    with full_db._connect() as conn:
        targets = [row["target_public_at"] for row in conn.execute(
            "SELECT target_public_at FROM planned_slots WHERE channel_id=10 AND status='pending' ORDER BY id")]
    assert sorted(targets) == sorted([shared_target, "2030-01-02T11:00:00+00:00"])


def test_new_slot_colliding_with_occupied_target_is_dropped(full_db, monkeypatch):
    """(c) A 'new' long-form item must not claim an occupied target.

    The second proposed slot shares the retained slot's target; it disappears
    from the review and counts["new"] does not count it.
    """
    occupied_target = "2030-01-02T11:00:00+00:00"
    with full_db._connect() as conn:
        conn.execute(_PENDING_SLOT, (10, "2030-01-02", "2030-01-02 08:00:00",
                                     "2030-01-02 10:00:00", occupied_target))
        conn.commit()

    preflight, applied = _preflight_and_apply(monkeypatch, full_db, [
        # Identical to the pending row -> retained (keeps occupying the target).
        _proposal(10, "2030-01-02", "2030-01-02 08:00:00", occupied_target,
                  target_upload_at="2030-01-02 10:00:00"),
        # Extra capacity -> 'new' slot that would collide with the target.
        _proposal(10, "2030-01-03", "2030-01-03 20:00:00", occupied_target),
    ])

    assert preflight["counts"]["new"] == 0
    assert preflight["counts"]["retained"] == 1
    assert all(it["slot_id"] is not None for it in preflight["review"])  # no orphan new item
    assert applied["ok"] is True
    with full_db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM planned_slots WHERE channel_id=10 AND status='pending'").fetchone()[0]
    assert n == 1


def test_resolver_keeps_shorts_untouched():
    """(d) Shorts are not covered by the long-form target index: never degrade.

    A short rescheduled item keeps its proposed retime even when a long-form
    item on the very same target is degraded because that target is occupied.
    """
    review = [
        # Short slated to move onto T: unaffected by any long-form collision.
        {"kind": "short", "action": "rescheduled", "slot_id": 1, "channel_id": 10,
         "before": {"target_public_at": "2030-01-01T11:00:00+00:00"},
         "after": {"target_public_at": "T"}},
        # Retained long slot holds T from the start of the apply transaction.
        {"kind": "long_form", "action": "retained", "slot_id": 2, "channel_id": 10,
         "before": {"target_public_at": "T"}, "after": {"target_public_at": "T"}},
        # Long reschedule onto T must degrade.
        {"kind": "long_form", "action": "rescheduled", "slot_id": 3, "channel_id": 10,
         "before": {"target_public_at": "2030-01-01T11:00:00+00:00"},
         "after": {"target_public_at": "T"}},
    ]
    counts = {"retained": 1, "rescheduled": 2, "new": 0}
    kept, new_counts = planning_service._resolve_public_target_collisions(review, counts)

    short = next(it for it in kept if it["kind"] == "short")
    degraded = next(it for it in kept if it["kind"] == "long_form" and it["action"] == "retained"
                    and it.get("reason") == "collision_active_slot")
    assert short["action"] == "rescheduled"  # untouched
    assert degraded["slot_id"] == 3
    # Counters only track long-form items; the short is not counted at all.
    assert new_counts == {"retained": 2, "rescheduled": 0, "new": 0}


def test_resolver_deterministic_apply_order_second_winner_is_degraded():
    """Resolver mirrors apply order: non-new first, then new (review order)."""
    # Two pendings both proposed onto T: first in review order wins the target.
    review = [
        {"kind": "long_form", "action": "rescheduled", "slot_id": 1, "channel_id": 10,
         "before": {"target_public_at": "A"}, "after": {"target_public_at": "T"}},
        {"kind": "long_form", "action": "rescheduled", "slot_id": 2, "channel_id": 10,
         "before": {"target_public_at": "B"}, "after": {"target_public_at": "T"}},
    ]
    kept, counts = planning_service._resolve_public_target_collisions(
        review, {"retained": 0, "rescheduled": 2, "new": 0})
    assert [it["action"] for it in kept] == ["rescheduled", "retained"]
    assert kept[0]["after"]["target_public_at"] == "T"
    assert kept[1]["reason"] == "collision_active_slot"
    assert counts == {"retained": 1, "rescheduled": 1, "new": 0}


def test_resolver_blocks_cross_move_onto_later_slots_current_target():
    """Earlier items cannot claim a target a later rescheduled slot still owns.

    Seed includes the current targets of every rescheduled slot: when item 1
    tries to move onto B (still held by item 2 until its own UPDATE), it must
    degrade exactly as the sequential UPDATEs in apply would fail.
    """
    review = [
        {"kind": "long_form", "action": "rescheduled", "slot_id": 1, "channel_id": 7,
         "before": {"target_public_at": "A"}, "after": {"target_public_at": "B"}},
        {"kind": "long_form", "action": "rescheduled", "slot_id": 2, "channel_id": 7,
         "before": {"target_public_at": "B"}, "after": {"target_public_at": "C"}},
    ]
    kept, counts = planning_service._resolve_public_target_collisions(
        review, {"retained": 0, "rescheduled": 2, "new": 0})
    assert [it["action"] for it in kept] == ["retained", "rescheduled"]
    assert kept[0]["reason"] == "collision_active_slot"
    assert kept[0]["after"] == kept[0]["before"]
    assert kept[1]["after"]["target_public_at"] == "C"
    assert counts == {"retained": 1, "rescheduled": 1, "new": 0}


def test_resolver_allows_taking_a_target_freed_by_an_earlier_move():
    """A slot that already moved away frees its old target for later items.

    The deterministic walk releases the old target only after the owning slot's
    move succeeds, mirroring the sequential UPDATEs of apply.
    """
    review = [
        {"kind": "long_form", "action": "rescheduled", "slot_id": 1, "channel_id": 7,
         "before": {"target_public_at": "A"}, "after": {"target_public_at": "FREE"}},
        {"kind": "long_form", "action": "rescheduled", "slot_id": 2, "channel_id": 7,
         "before": {"target_public_at": "B"}, "after": {"target_public_at": "A"}},
    ]
    kept, counts = planning_service._resolve_public_target_collisions(
        review, {"retained": 0, "rescheduled": 2, "new": 0})
    assert [it["action"] for it in kept] == ["rescheduled", "rescheduled"]
    assert [it["after"]["target_public_at"] for it in kept] == ["FREE", "A"]
    assert counts == {"retained": 0, "rescheduled": 2, "new": 0}


def test_resolver_drops_new_item_only_on_collision():
    review = [
        {"kind": "long_form", "action": "retained", "slot_id": 1, "channel_id": 10,
         "before": {"target_public_at": "T"}, "after": {"target_public_at": "T"}},
        {"kind": "long_form", "action": "new", "slot_id": None, "channel_id": 10,
         "before": None, "after": {"target_public_at": "T"}},      # collision -> dropped
        {"kind": "long_form", "action": "new", "slot_id": None, "channel_id": 10,
         "before": None, "after": {"target_public_at": "FREE"}},   # free -> kept
    ]
    kept, counts = planning_service._resolve_public_target_collisions(
        review, {"retained": 1, "rescheduled": 0, "new": 2})
    assert len(kept) == 2
    assert counts == {"retained": 1, "rescheduled": 0, "new": 1}
    assert kept[-1]["after"]["target_public_at"] == "FREE"


def test_apply_turns_active_target_collision_into_stale_value_error(full_db, monkeypatch):
    """Safety net: a stale plan that still collides must surface as a reviewable
    ValueError (router 409 SAFE_REPLAN_STALE), never a raw IntegrityError 500."""
    running_target = "2030-01-01T11:00:00+00:00"
    old_target = "2030-01-02T11:00:00+00:00"
    with full_db._connect() as conn:
        conn.execute(_RUNNING_SLOT, (10, "2030-01-01", "2030-01-01 08:00:00",
                                     "2030-01-01 10:00:00", running_target))
        conn.execute(_PENDING_SLOT, (10, "2030-01-02", "2030-01-02 08:00:00",
                                     "2030-01-02 10:00:00", old_target))
        conn.commit()

    monkeypatch.setattr(planning_service, "_safe_replan_proposed_shorts", lambda d, h: [])
    monkeypatch.setattr(planning_service, "_safe_replan_proposed_slots", lambda d, h: [
        _proposal(10, "2030-01-02", "2030-01-02 08:00:00", running_target),
    ])
    # Preflight only: the fixed resolver already marks the item retained.
    preflight = planning_service.safe_full_replan_preflight(db=full_db, horizon_days=7)
    assert all(it["action"] != "rescheduled" for it in preflight["review"])
    # Simulate a pre-fix confirmation: rewrite the stored plan so the pending
    # slot is rescheduled onto the running slot's target again.
    confirmation = preflight["confirmation_token"]
    confirmation_hash = hashlib.sha256(confirmation.encode("utf-8")).hexdigest()
    with full_db._connect() as conn:
        stored = json.loads(conn.execute(
            "SELECT plan_json FROM safe_replan_confirmations WHERE token_hash=?", (confirmation_hash,)).fetchone()[0])
        for item in stored["review"]:
            if item["action"] == "retained" and item["slot_id"] is not None:
                item["action"] = "rescheduled"
                item["reason"] = "backlog_prioritized"
                after = dict(item["before"])
                after["target_public_at"] = running_target
                item["after"] = after
        stored["counts"] = {"retained": 0, "rescheduled": 1, "new": 0}
        conn.execute("UPDATE safe_replan_confirmations SET plan_json=? WHERE token_hash=?",
                     (json.dumps(stored), confirmation_hash))
        conn.commit()

    with pytest.raises(ValueError, match="replan plan collides with an active slot"):
        planning_service.safe_full_replan_apply(confirmation, db=full_db)

    with full_db._connect() as conn:
        pending = conn.execute("SELECT target_public_at, status FROM planned_slots "
                               "WHERE channel_id=10 AND status='pending'").fetchone()
        unused = conn.execute("SELECT used_at FROM safe_replan_confirmations WHERE token_hash=?",
                              (confirmation_hash,)).fetchone()
    assert pending["target_public_at"] == old_target  # rolled back
    assert unused["used_at"] is None


def test_replan_router_maps_collision_value_error_to_409_stale(monkeypatch):
    from fastapi import HTTPException
    from api.routers import planning

    monkeypatch.setattr(planning, "get_db", lambda: object())
    monkeypatch.setattr(
        planning_service,
        "safe_full_replan_apply",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("replan plan collides with an active slot; re-run the review")),
    )

    with pytest.raises(HTTPException) as exc_info:
        planning.safe_full_replan_apply(planning.SafeFullReplanApply(confirmation_token="token"))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "SAFE_REPLAN_STALE"
