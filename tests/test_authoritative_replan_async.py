"""Async lifecycle tests for the authoritative replan coordinator.

The operator endpoint must return a persistent run immediately while the
repack continues in the background. These tests deliberately block the runner
to prove that HTTP work is not coupled to the replan duration.
"""

import json
import subprocess
import sys
import threading
import time

import pytest

from api.services.authoritative_replan_service import (
    MAX_RECOVERY_ATTEMPTS,
    STATE_KEY,
    AuthoritativeReplanCoordinator,
    ReplanNotRecoverable,
    _proc_start_ticks,
    _process_identity,
)


class _MemoryDB:
    def __init__(self, state=None):
        self._state = dict(state or {})
        self._lock = threading.Lock()

    def get_system_state(self, key):
        with self._lock:
            return self._state.get(key)

    def set_system_state(self, key, value):
        with self._lock:
            self._state[key] = value

    def persisted(self):
        with self._lock:
            return json.loads(self._state[STATE_KEY])


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_start_returns_immediately_and_joins_active_run():
    started = threading.Event()
    release = threading.Event()

    def runner(db, horizon_days, progress):
        progress("repack", current_channel_slug="canal2",
                 completed_channels=0, total_channels=2)
        started.set()
        assert release.wait(3), "test runner was not released"
        progress("finalizing", current_channel_slug=None,
                 completed_channels=2, total_channels=2)
        return {
            "ok": True,
            "slots_cleared": 4,
            "videos_retimed": 3,
            "today_forced": 1,
            "jobs_cancelled": 2,
        }

    db = _MemoryDB()
    coord = AuthoritativeReplanCoordinator(runner=runner)

    t0 = time.monotonic()
    first = coord.start(db=db, horizon_days=7)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5, "start() must not wait for the replan itself"
    assert first["run_id"]
    assert first["queued"] is True
    assert first["joined"] is False
    assert first["status"] in ("queued", "running")
    assert started.wait(2), "background runner never started"

    second = coord.start(db=db, horizon_days=7)
    assert second["run_id"] == first["run_id"], "active run must be joined"
    assert second["joined"] is True

    release.set()
    assert _wait_for(
        lambda: coord.snapshot(db=db)["status"] == "success",
    ), "run did not reach terminal success"
    final = coord.snapshot(db=db)
    assert final["ok"] is True
    assert final["summary"]["slots_cleared"] == 4
    assert final["phase"] == "done"
    assert final["completed_channels"] == 2
    assert db.persisted()["status"] == "success"


def test_runner_failure_is_persisted_as_error():
    def runner(db, horizon_days, progress):
        progress("repack", completed_channels=0, total_channels=1)
        raise RuntimeError("youtube update failed")

    db = _MemoryDB()
    coord = AuthoritativeReplanCoordinator(runner=runner)
    coord.start(db=db)

    assert _wait_for(lambda: coord.snapshot(db=db)["status"] == "error")
    final = coord.snapshot(db=db)
    assert final["queued"] is False
    assert "youtube update failed" in final["error"]
    assert db.persisted()["status"] == "error"


def test_recover_on_startup_marks_interrupted_but_does_not_autostart():
    calls = []

    def runner(db, horizon_days, progress):
        calls.append(horizon_days)
        return {"ok": True}

    db = _MemoryDB({
        STATE_KEY: json.dumps({
            "run_id": "stale-run",
            "status": "running",
            "queued": True,
            "phase": "repack",
            "pid": 999999,
            "pid_start_ticks": 1,
        })
    })
    coord = AuthoritativeReplanCoordinator(runner=runner)
    recovered = coord.recover_on_startup(db=db)

    assert recovered["run_id"] == "stale-run"
    assert recovered["status"] == "interrupted"
    assert recovered["queued"] is False
    assert recovered["recoverable"] is True
    assert recovered["recovery_available"] is True
    assert recovered["owner_alive"] is False
    assert recovered["interrupt_reason"] == "server_restart"
    assert "restart" in recovered["error"].lower()
    assert db.persisted()["status"] == "interrupted"
    assert calls == [], "recovery must be explicit, never silent"


def test_explicit_recovery_starts_a_new_attempt():
    started = threading.Event()
    release = threading.Event()

    def runner(db, horizon_days, progress):
        started.set()
        assert release.wait(3)
        return {"ok": True, "slots_cleared": 1}

    db = _MemoryDB({
        STATE_KEY: json.dumps({
            "run_id": "interrupted-run",
            "status": "running",
            "phase": "repack",
            "horizon_days": 5,
            "pid": 999999,
            "pid_start_ticks": 1,
        })
    })
    coord = AuthoritativeReplanCoordinator(runner=runner)
    coord.recover_on_startup(db=db)

    resumed = coord.recover(db=db)
    assert resumed["recovered"] is True
    assert resumed["joined"] is False
    assert resumed["run_id"] != "interrupted-run"
    assert resumed["recovered_from"] == "interrupted-run"
    assert resumed["recovery_attempts"] == 1
    assert resumed["horizon_days"] == 5
    assert started.wait(2)

    release.set()
    assert _wait_for(lambda: coord.snapshot(db=db)["status"] == "success")
    assert db.persisted()["recovered_from"] == "interrupted-run"


def test_recovery_is_bounded_and_rejects_terminal_runs():
    db = _MemoryDB({
        STATE_KEY: json.dumps({
            "run_id": "exhausted-run",
            "status": "interrupted",
            "phase": "interrupted",
            "recovery_attempts": MAX_RECOVERY_ATTEMPTS,
            "recoverable": True,
            "pid": 999999,
            "pid_start_ticks": 1,
        })
    })
    coord = AuthoritativeReplanCoordinator(runner=lambda **_kwargs: {"ok": True})
    with pytest.raises(ReplanNotRecoverable) as exc:
        coord.recover(db=db)
    assert exc.value.code == "REPLAN_RECOVERY_EXHAUSTED"

    success_db = _MemoryDB({
        STATE_KEY: json.dumps({
            "run_id": "done-run",
            "status": "success",
            "phase": "done",
        })
    })
    done = AuthoritativeReplanCoordinator(runner=lambda **_kwargs: {"ok": True})
    with pytest.raises(ReplanNotRecoverable) as exc2:
        done.recover(db=success_db)
    assert exc2.value.code == "REPLAN_NOT_RECOVERABLE"


def test_start_reclaims_an_orphaned_active_run():
    old = {
        "run_id": "ghost-run",
        "status": "running",
        "queued": True,
        "phase": "repack",
        "pid": 999999,
        "pid_start_ticks": 1,
    }
    db = _MemoryDB({STATE_KEY: json.dumps(old)})
    coord = AuthoritativeReplanCoordinator(runner=lambda **_kwargs: {"ok": True})

    started = coord.start(db=db)
    assert started["run_id"] != "ghost-run"
    assert started["status"] in ("queued", "running")
    assert db.persisted()["run_id"] == started["run_id"]


def test_recover_on_startup_preserves_a_live_owner_in_another_process():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
    )
    try:
        deadline = time.time() + 2
        ticks = None
        while time.time() < deadline:
            ticks = _proc_start_ticks(child.pid)
            if ticks is not None:
                break
            time.sleep(0.01)
        assert ticks is not None

        db = _MemoryDB({
            STATE_KEY: json.dumps({
                "run_id": "live-run",
                "status": "running",
                "queued": True,
                "phase": "repack",
                "pid": child.pid,
                "pid_start_ticks": ticks,
            })
        })
        coord = AuthoritativeReplanCoordinator(runner=lambda **_kwargs: {"ok": True})
        recovered = coord.recover_on_startup(db=db)

        assert recovered["status"] == "running"
        assert recovered["owner_alive"] is True
        assert recovered["orphaned"] is False
        assert recovered["recoverable"] is False
        assert db.persisted()["status"] == "running"

        joined = coord.start(db=db)
        assert joined["run_id"] == "live-run"
        assert joined["joined"] is True
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_snapshot_reports_live_owner_and_staleness():
    owner = _process_identity()
    old_heartbeat = time.strftime(
        "%Y-%m-%dT%H:%M:%S+00:00",
        time.gmtime(time.time() - 7200),
    )
    db = _MemoryDB({
        STATE_KEY: json.dumps({
            "run_id": "slow-run",
            "status": "running",
            "queued": True,
            "phase": "repack",
            "heartbeat_at": old_heartbeat,
            **owner,
        })
    })
    coord = AuthoritativeReplanCoordinator()
    snap = coord.snapshot(db=db)

    assert snap["owner_alive"] is True
    assert snap["orphaned"] is False
    assert snap["status"] == "running"
    assert snap["stale"] is True
    assert snap["stale_after_s"] > 0
    assert snap["heartbeat_age_s"] > snap["stale_after_s"]
    assert snap["recoverable"] is False
    # Diagnostics on GET must never mutate or release a live run.
    assert db.persisted()["status"] == "running"
