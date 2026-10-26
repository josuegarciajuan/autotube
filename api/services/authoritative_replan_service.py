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
  - A run interrupted by an API restart is marked as an error on startup. The
    operation is idempotent, so the operator can safely launch it again.

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


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_state() -> dict:
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
        "summary": None,
        "error": None,
        "pid": os.getpid(),
    }


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

            if (
                self._state.get("status") in _ACTIVE_STATUSES
                and self._thread is not None
                and self._thread.is_alive()
            ):
                snapshot = self._snapshot_locked()
                snapshot.update({"queued": True, "joined": True})
                return snapshot

            if self._state.get("status") in _ACTIVE_STATUSES:
                # A persisted active state without a live owner belongs to a
                # previous process (or a thread that died before its handler).
                # Never leave the panel permanently blocked by it.
                self._state.update({
                    "ok": False,
                    "status": "error",
                    "queued": False,
                    "error": "Interrupted by server restart",
                    "finished_at": _utcnow_iso(),
                    "heartbeat_at": _utcnow_iso(),
                    "phase": "interrupted",
                })
                self._pin_locked(db)

            run_id = uuid.uuid4().hex[:12]
            now = _utcnow_iso()
            self._state = {
                **_fresh_state(),
                "run_id": run_id,
                "status": "queued",
                "queued": True,
                "created_at": now,
                "heartbeat_at": now,
                "horizon_days": int(horizon_days),
                "phase": "queued",
                "pid": os.getpid(),
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
            snapshot.update({"queued": True, "joined": False})
            logger.info(
                "authoritative replan %s queued (horizon=%dd)", run_id, horizon_days
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
            if state.get("status") in _ACTIVE_STATUSES:
                state.update({
                    "ok": False,
                    "status": "error",
                    "queued": False,
                    "error": "Interrupted by server restart",
                    "finished_at": _utcnow_iso(),
                    "heartbeat_at": _utcnow_iso(),
                    "phase": "interrupted",
                })
                logger.warning(
                    "authoritative replan %s interrupted by restart",
                    state.get("run_id"),
                )
            self._state = state
            self._thread = None
            self._loaded = True
            self._pin_locked(db)
            return self._snapshot_locked()

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
            )

    # ── State helpers ─────────────────────────────────────────

    def _transition(self, run_id: str, db, **updates: Any) -> None:
        with self._mutex:
            if self._state.get("run_id") != run_id:
                return
            self._state.update(updates)
            self._pin_locked(db)

    def _snapshot_locked(self) -> dict:
        state = {**self._state}
        state["summary"] = self._state.get("summary")
        state["ok"] = bool(
            self._state.get("ok")
            and self._state.get("status") not in ("error", "not_found")
        )
        now = time.time()
        for key in ("created_at", "started_at", "heartbeat_at"):
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
        state["elapsed_s"] = state.get("started_at_age_s") if state.get("started_at") else None
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
