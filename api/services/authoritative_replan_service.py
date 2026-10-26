"""Async lifecycle for the authoritative "Reprogramar Ahora" replan.

The operator action is long-running: it repacks every active channel, updates
YouTube publish times and rebuilds the long-form/Shorts horizon. Keeping that
work inside the HTTP request made Apache/gateway timeouts surface as a proxy
error while the backend was still running and eventually completed.

This coordinator decouples the request from the work:

  - ``start()`` persists a run and returns immediately (HTTP 202).
  - A daemon thread executes ``authoritative_replan`` in the API process. That
    preserves the existing ``_REPLAN_LOCK`` contract with the automatic
    planning loops, so only one replan writer runs at a time.
  - Every stage is persisted in ``system_state['authoritative_replan_state']``
    (queue, channel repack, horizon, shorts, terminal result/error).
  - The owner is identified by PID + Linux start ticks, so PID reuse cannot
    turn a dead run into a false live owner. Snapshots distinguish live,
    orphaned, stale and interrupted runs.
  - Snapshots include coarse overall progress, phase, current channel and the
    per-channel completion summaries consumed by the live modal polling.
  - A run interrupted by an API restart becomes ``interrupted`` and exposes an
    explicit, bounded recovery action. It is never retried silently.

The coordinator deliberately keeps only the latest run. It is an operational
command, not an audit log; terminal results remain available until the next
run starts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("autotube.replan")

STATE_KEY = "authoritative_replan_state"
_ACTIVE_STATUSES = ("queued", "running")
_ERROR_MAX_CHARS = 2000

def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# A run is only a candidate for recovery when it was interrupted, never when
# it failed its own work. A second restart while recovering must not loop
# forever without an operator decision.
MAX_RECOVERY_ATTEMPTS = _env_int(
    "AUTHORITATIVE_REPLAN_MAX_RECOVERY_ATTEMPTS", 1, minimum=0
)

# Stage callbacks can be separated by a long per-channel repack (minutes).
# Treat this as a diagnostic threshold, not as permission to kill the thread.
STALE_AFTER_S = _env_int(
    "AUTHORITATIVE_REPLAN_STALE_S", 1800, minimum=60
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proc_start_ticks(pid: int) -> Optional[int]:
    """Return Linux /proc start time ticks for PID, or None if it is gone.

    ``/proc/<pid>/stat`` field 22 distinguishes a live owner from a reused PID.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8") as handle:
            raw = handle.read()
        close_paren = raw.rfind(")")
        if close_paren < 0:
            return None
        fields = raw[close_paren + 2:].split()
        # After comm, field 3 is state; starttime is field 22 overall.
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _process_identity() -> dict:
    pid = os.getpid()
    return {
        "pid": pid,
        "pid_start_ticks": _proc_start_ticks(pid),
    }


def _fresh_state() -> dict:
    owner = _process_identity()
    return {
        "ok": True,
        "run_id": None,
        "status": "idle",
        "queued": False,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "heartbeat_at": None,
        "phase": None,
        "horizon_days": 7,
        "current_channel_slug": None,
        "completed_channels": 0,
        "total_channels": 0,
        "channels": [],
        "progress_pct": 0,
        "yt_updates_attempted": 0,
        "yt_updates_succeeded": 0,
        "yt_updates_failed": 0,
        "yt_updates_limited": 0,
        "yt_update_budget": None,
        "partial": False,
        "summary": None,
        "error": None,
        # Owner/lease: pid_start_ticks protects against PID reuse across boots.
        "pid": owner["pid"],
        "pid_start_ticks": owner["pid_start_ticks"],
        "owner_alive": True,
        "orphaned": False,
        "stale": False,
        "stale_after_s": STALE_AFTER_S,
        # Recovery metadata (explicit, finite).
        "attempt": 0,
        "recovery_attempts": 0,
        "recovered_from": None,
        "interrupted_at": None,
        "interrupt_reason": None,
        "recoverable": False,
        "recovery_available": False,
    }


class ReplanNotRecoverable(RuntimeError):
    """The latest run is not an interrupted run eligible for recovery."""

    def __init__(self, message: str, code: str, snapshot: dict):
        super().__init__(message)
        self.code = code
        self.snapshot = snapshot


class AuthoritativeReplanCoordinator:
    """Single-flight persistent lifecycle for the authoritative replan."""

    def __init__(
        self,
        runner: Optional[Callable[..., dict]] = None,
    ) -> None:
        self._mutex = threading.Lock()
        self._state: dict = _fresh_state()
        self._thread: Optional[threading.Thread] = None
        self._loaded = False
        self._runner = runner

    # ── Public API ────────────────────────────────────────────

    def start(self, db, horizon_days: int = 7) -> dict:
        """Queue a run (or join the active one) and return its snapshot."""
        with self._mutex:
            self._ensure_loaded_locked(db)

            if self._state.get("status") in _ACTIVE_STATUSES:
                # Same process + live worker: never start a second writer.
                if self._thread is not None and self._thread.is_alive():
                    snapshot = self._snapshot_locked()
                    snapshot.update({"queued": True, "joined": True})
                    return snapshot

                # Another live process owns the persisted run. Joining is
                # safer than interrupting a job this process cannot see.
                if (
                    int(self._state.get("pid") or 0) != os.getpid()
                    and self._owner_alive(self._state)
                ):
                    snapshot = self._snapshot_locked()
                    snapshot.update({"queued": True, "joined": True})
                    return snapshot

                # No live owner: reclaim the ghost run before starting anew.
                self._mark_interrupted_locked(
                    db,
                    reason="owner_not_alive",
                    message="Interrupted: previous worker is no longer alive",
                )

            return self._start_locked(
                db, horizon_days=horizon_days, joined=False,
            )

    def recover(
        self,
        db,
        run_id: Optional[str] = None,
        horizon_days: Optional[int] = None,
    ) -> dict:
        """Explicitly retry the latest interrupted run, at most once.

        Recovery is intentionally not automatic: an API restart can happen at
        any point, and the operator must be able to decide when to resume a
        side-effecting operation (YouTube publishAt updates). The retry is
        still bounded by ``MAX_RECOVERY_ATTEMPTS`` so a crash loop cannot
        hammer the scheduler forever.
        """
        with self._mutex:
            self._ensure_loaded_locked(db)

            if run_id and self._state.get("run_id") != run_id:
                raise ReplanNotRecoverable(
                    "Replan run not found (only the latest run is retained)",
                    "REPLAN_NOT_FOUND",
                    self._snapshot_locked(),
                )

            if self._state.get("status") in _ACTIVE_STATUSES:
                if self._thread is not None and self._thread.is_alive():
                    snapshot = self._snapshot_locked()
                    snapshot.update({"queued": True, "joined": True, "recovered": False})
                    return snapshot
                if (
                    int(self._state.get("pid") or 0) != os.getpid()
                    and self._owner_alive(self._state)
                ):
                    snapshot = self._snapshot_locked()
                    snapshot.update({"queued": True, "joined": True, "recovered": False})
                    return snapshot
                self._mark_interrupted_locked(
                    db,
                    reason="owner_not_alive",
                    message="Interrupted: previous worker is no longer alive",
                )

            legacy_interrupted = (
                self._state.get("status") == "error"
                and self._state.get("phase") == "interrupted"
            )
            if self._state.get("status") != "interrupted" and not legacy_interrupted:
                raise ReplanNotRecoverable(
                    "The latest authoritative replan is not recoverable",
                    "REPLAN_NOT_RECOVERABLE",
                    self._snapshot_locked(),
                )

            attempts = int(self._state.get("recovery_attempts") or 0)
            if attempts >= MAX_RECOVERY_ATTEMPTS:
                raise ReplanNotRecoverable(
                    "Maximum recovery attempts reached for this replan",
                    "REPLAN_RECOVERY_EXHAUSTED",
                    self._snapshot_locked(),
                )

            previous_run = self._state.get("run_id")
            effective_horizon = int(
                horizon_days or self._state.get("horizon_days") or 7
            )
            snapshot = self._start_locked(
                db,
                horizon_days=effective_horizon,
                joined=False,
                recovered_from=previous_run,
                recovery_attempts=attempts + 1,
            )
            snapshot["recovered"] = True
            logger.warning(
                "authoritative replan %s recovered from interrupted run %s "
                "(attempt %d/%d)",
                snapshot.get("run_id"), previous_run,
                attempts + 1, MAX_RECOVERY_ATTEMPTS,
            )
            return snapshot

    def snapshot(self, db=None, run_id: Optional[str] = None) -> dict:
        """Return the latest run state; never mutates lifecycle on read."""
        with self._mutex:
            if db is not None:
                self._ensure_loaded_locked(db)
            snapshot = self._snapshot_locked()
        if run_id and snapshot.get("run_id") != run_id:
            return {
                **_fresh_state(),
                "ok": False,
                "run_id": run_id,
                "status": "not_found",
                "error": "Replan run not found (only the latest run is retained)",
            }
        return snapshot

    def recover_on_startup(self, db=None) -> dict:
        """Restore persisted state and fail runs whose owner died with the API."""
        with self._mutex:
            if db is None:
                from database.db_extended import ExtendedDatabase
                db = ExtendedDatabase()
            saved = self._read_persisted_locked(db)
            if not saved:
                self._loaded = True
                self._thread = None
                return self._snapshot_locked()

            state = {**_fresh_state(), **saved}
            self._state = state
            self._thread = None
            self._loaded = True
            another_live_owner = (
                int(state.get("pid") or 0) != os.getpid()
                and self._owner_alive(state)
            )
            if state.get("status") in _ACTIVE_STATUSES and not another_live_owner:
                self._mark_interrupted_locked(
                    db,
                    reason="server_restart",
                    message="Interrupted by server restart",
                )
                logger.warning(
                    "authoritative replan %s interrupted by restart; "
                    "explicit recovery available",
                    state.get("run_id"),
                )
            else:
                # Terminal state or a genuinely live owner in another process:
                # preserve it instead of inventing an interruption.
                self._pin_locked(db)
            return self._snapshot_locked()

    # ── Lifecycle helpers ─────────────────────────────────────

    def _start_locked(
        self,
        db,
        horizon_days: int,
        joined: bool,
        recovered_from: Optional[str] = None,
        recovery_attempts: int = 0,
    ) -> dict:
        run_id = uuid.uuid4().hex[:12]
        now = _utcnow_iso()
        owner = _process_identity()
        previous_attempt = int(self._state.get("attempt") or 0)
        self._state = {
            **_fresh_state(),
            "run_id": run_id,
            "status": "queued",
            "queued": True,
            "created_at": now,
            "heartbeat_at": now,
            "horizon_days": int(horizon_days),
            "phase": "queued",
            "attempt": previous_attempt + 1,
            "recovery_attempts": int(recovery_attempts),
            "recovered_from": recovered_from,
            **owner,
            "owner_alive": True,
        }
        self._pin_locked(db)

        thread = threading.Thread(
            target=self._execute,
            args=(run_id, db, int(horizon_days)),
            name=f"authoritative-replan-{run_id}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        snapshot = self._snapshot_locked()
        snapshot.update({"queued": True, "joined": joined})
        logger.info(
            "authoritative replan %s queued (horizon=%dd, attempt=%d, recovered_from=%s)",
            run_id, horizon_days, self._state.get("attempt"), recovered_from,
        )
        return snapshot

    def _mark_interrupted_locked(self, db, reason: str, message: str) -> None:
        now = _utcnow_iso()
        self._state.update({
            "ok": False,
            "status": "interrupted",
            "queued": False,
            "error": message,
            "finished_at": now,
            "heartbeat_at": now,
            "interrupted_at": now,
            "interrupt_reason": reason,
            "phase": "interrupted",
            "current_channel_slug": None,
            "owner_alive": False,
            "orphaned": True,
            "stale": False,
            "recoverable": True,
            "recovery_available": (
                int(self._state.get("recovery_attempts") or 0)
                < MAX_RECOVERY_ATTEMPTS
            ),
        })
        self._pin_locked(db)

    @staticmethod
    def _owner_alive(state: dict) -> bool:
        try:
            pid = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        current_ticks = _proc_start_ticks(pid)
        if current_ticks is None:
            return False
        expected = state.get("pid_start_ticks")
        if expected is not None:
            try:
                return int(expected) == int(current_ticks)
            except (TypeError, ValueError):
                return False
        # Legacy state (pre-lease) cannot prove ownership after PID reuse.
        return pid == os.getpid()

    # ── Worker ────────────────────────────────────────────────

    def _execute(self, run_id: str, db, horizon_days: int) -> None:
        self._transition(
            run_id,
            db,
            status="running",
            queued=True,
            phase="starting",
            started_at=_utcnow_iso(),
            heartbeat_at=_utcnow_iso(),
            owner_alive=True,
            orphaned=False,
            stale=False,
        )

        def progress(phase: str, **fields: Any) -> None:
            self._transition(
                run_id,
                db,
                phase=phase,
                heartbeat_at=_utcnow_iso(),
                **fields,
            )

        try:
            runner = self._runner
            if runner is None:
                from api.services.planning_service import authoritative_replan
                runner = authoritative_replan
            result = runner(db=db, horizon_days=horizon_days, progress=progress)
            ok = bool((result or {}).get("ok", True))
            final_status = "success" if ok else "blocked"
            self._transition(
                run_id,
                db,
                ok=ok,
                status=final_status,
                queued=False,
                phase="done",
                summary=result,
                finished_at=_utcnow_iso(),
                heartbeat_at=_utcnow_iso(),
                current_channel_slug=None,
                error=None,
                recoverable=False,
                recovery_available=False,
                interrupt_reason=None,
            )
            logger.info(
                "authoritative replan %s finished: status=%s slots=%s videos=%s",
                run_id,
                final_status,
                (result or {}).get("slots_cleared"),
                (result or {}).get("videos_retimed"),
            )
        except Exception as exc:
            logger.error(
                "authoritative replan %s failed: %s", run_id, exc, exc_info=True
            )
            self._transition(
                run_id,
                db,
                ok=False,
                status="error",
                queued=False,
                phase="error",
                error=str(exc)[:_ERROR_MAX_CHARS],
                finished_at=_utcnow_iso(),
                heartbeat_at=_utcnow_iso(),
                current_channel_slug=None,
                recoverable=False,
                recovery_available=False,
                interrupt_reason=None,
            )

    # ── State helpers ─────────────────────────────────────────

    def _transition(self, run_id: str, db, **updates: Any) -> None:
        with self._mutex:
            if self._state.get("run_id") != run_id:
                return
            channel_summary = updates.pop("channel_summary", None)
            self._state.update(updates)
            if isinstance(channel_summary, dict) and channel_summary:
                channels = list(self._state.get("channels") or [])
                slug = channel_summary.get("slug")
                # Progress callbacks are idempotent; never duplicate a channel.
                if not slug or not any(c.get("slug") == slug for c in channels):
                    channels.append(channel_summary)
                    self._state["channels"] = channels
            self._state["progress_pct"] = self._progress_pct(self._state)
            self._pin_locked(db)

    @staticmethod
    def _progress_pct(state: dict) -> int:
        """Coarse overall progress derived from phase + completed channels."""
        status = state.get("status")
        phase = state.get("phase")
        if status == "success" or phase == "done":
            return 100
        if status == "queued" or phase == "queued":
            return 0
        try:
            completed = max(0, int(state.get("completed_channels") or 0))
            total = max(0, int(state.get("total_channels") or 0))
        except (TypeError, ValueError):
            completed, total = 0, 0
        if phase == "starting":
            return 5
        if phase in ("repack", "coverage", "channel_done"):
            base = 10 + (70 * completed // total if total else 0)
            return min(80, base)
        if phase == "horizon":
            return 85
        if phase == "shorts":
            return 90
        if phase == "finalizing":
            return 97
        if phase in ("interrupted", "error"):
            return min(95, 10 + (70 * completed // total if total else 0))
        return 0

    def _snapshot_locked(self) -> dict:
        state = {**self._state}
        state["summary"] = self._state.get("summary")
        state["channels"] = list(self._state.get("channels") or [])
        state["progress_pct"] = self._progress_pct(self._state)
        state["ok"] = bool(
            self._state.get("ok")
            and self._state.get("status") not in ("error", "not_found")
        )
        now = time.time()
        for key in ("created_at", "started_at", "heartbeat_at", "finished_at"):
            age_key = f"{key}_age_s"
            state[age_key] = None
            raw = self._state.get(key)
            if raw:
                try:
                    parsed = datetime.fromisoformat(str(raw))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    state[age_key] = max(0, int(now - parsed.timestamp()))
                except (TypeError, ValueError):
                    pass
        duration_s = None
        if (
            state.get("started_at_age_s") is not None
            and state.get("finished_at_age_s") is not None
        ):
            duration_s = max(
                0,
                state["started_at_age_s"] - state["finished_at_age_s"],
            )
        state["duration_s"] = duration_s
        status = self._state.get("status")
        elapsed = state.get("started_at_age_s") if status in _ACTIVE_STATUSES else duration_s
        state["elapsed_s"] = elapsed
        state["heartbeat_age_s"] = state.get("heartbeat_at_age_s")
        heartbeat_age = state.get("heartbeat_age_s")
        active = status in _ACTIVE_STATUSES
        owner_alive = self._owner_alive(self._state) if active else False
        state["owner_alive"] = owner_alive
        state["orphaned"] = bool(active and not owner_alive)
        state["stale"] = bool(
            status == "running"
            and heartbeat_age is not None
            and heartbeat_age > STALE_AFTER_S
        )
        state["stale_after_s"] = STALE_AFTER_S
        state["max_recovery_attempts"] = MAX_RECOVERY_ATTEMPTS
        state["recoverable"] = bool(
            status == "interrupted"
            or (status == "error" and self._state.get("phase") == "interrupted")
        )
        state["recovery_available"] = bool(
            state["recoverable"]
            and int(self._state.get("recovery_attempts") or 0)
            < MAX_RECOVERY_ATTEMPTS
        )
        return state

    def _ensure_loaded_locked(self, db) -> None:
        if self._loaded:
            return
        self._state = {**_fresh_state(), **(self._read_persisted_locked(db) or {})}
        self._loaded = True

    @staticmethod
    def _read_persisted_locked(db) -> Optional[dict]:
        try:
            raw = db.get_system_state(STATE_KEY)
            if not raw:
                return None
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:
            logger.warning("Could not read authoritative replan state: %s", exc)
            return None

    def _pin_locked(self, db) -> None:
        try:
            db.set_system_state(
                STATE_KEY, json.dumps(self._state, ensure_ascii=False, default=str)
            )
        except Exception as exc:
            # Persistence failure must never drop the in-memory run.
            logger.warning("Could not persist authoritative replan state: %s", exc)


_coordinator: Optional[AuthoritativeReplanCoordinator] = None


def get_authoritative_replan_coordinator() -> AuthoritativeReplanCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = AuthoritativeReplanCoordinator()
    return _coordinator
