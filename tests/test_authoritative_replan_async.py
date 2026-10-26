"""Async lifecycle tests for the authoritative replan coordinator.

The operator endpoint must return a persistent run immediately while the
repack continues in the background. These tests deliberately block the runner
to prove that HTTP work is not coupled to the replan duration.
"""

import json
import threading
import time

from api.services.authoritative_replan_service import (
    STATE_KEY,
    AuthoritativeReplanCoordinator,
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


def test_recover_marks_interrupted_run_as_error():
    db = _MemoryDB({
        STATE_KEY: json.dumps({
            "run_id": "stale-run",
            "status": "running",
            "queued": True,
            "phase": "repack",
        })
    })
    coord = AuthoritativeReplanCoordinator(runner=lambda **_kwargs: {"ok": True})
    recovered = coord.recover_on_startup(db=db)

    assert recovered["run_id"] == "stale-run"
    assert recovered["status"] == "error"
    assert recovered["queued"] is False
    assert "restart" in recovered["error"].lower()
    assert db.persisted()["status"] == "error"

    # A new operator action must be able to start after the interruption.
    next_run = coord.start(db=db)
    assert next_run["status"] in ("queued", "running")
    assert next_run["run_id"] != "stale-run"
