#!/usr/bin/env python3
"""Decide whether it is safe to restart the API (``apply_changes.sh``) while
long-form generation is active.

Context (bug fixed sep 2026)
----------------------------
``apply_changes.sh`` had two contradictory blocks: one that correctly allowed a
restart when the active job ran as a *subprocess* worker (which survives the API
restart), and a later blanket abort that blocked the deploy whenever ANY
``full_pipeline_worker`` process existed. The blanket abort made the correct
logic dead code and, worse, returned ``exit 0``: the post-merge auto-deploy hook
silently did nothing and production kept running stale code.

The real reason restarts used to kill workers (systemd ``KillMode=mixed``
SIGKILLing the whole cgroup) was already fixed in ``autotube-panel.service``
(``KillMode=process``), so the blanket abort is obsolete.

This helper centralizes the decision:

* No running long-form job                      -> proceed.
* All running jobs run as subprocess workers     -> proceed (KillMode=process
  lets them survive the restart).
* Any running job is in-process (legacy mode)    -> abort, unless
  ``SKIP_ACTIVE_WORKER_CHECK=true`` forces it, or the operator confirms
  interactively (TTY only).

Exit codes: ``0`` = safe to deploy, ``1`` = blocked.

Usage:
    python3 scripts/deploy_safety.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable, Iterable, Optional

# Worker process pattern. Long-form workers are spawned as
# ``full_pipeline_worker.py --job-id <N>`` in their own session.
_WORKER_PATTERN = "full_pipeline_worker.*--job-id {job_id}"


def running_longform_jobs(db_path: Optional[str] = None) -> list[int]:
    """Running long-form job ids. Fail-open: [] on any DB error."""
    try:
        import sqlite3

        from config.settings import DATABASE_PATH

        path = db_path or DATABASE_PATH
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT id FROM generation_jobs WHERE status = 'running'"
            ).fetchall()
        finally:
            conn.close()
        return [int(r[0]) for r in rows]
    except Exception:
        return []


def worker_pid(job_id: int) -> Optional[int]:
    """PID of the subprocess worker for ``job_id``, or None if not found."""
    import subprocess
    try:
        out = subprocess.run(
            ["pgrep", "-f", _WORKER_PATTERN.format(job_id=int(job_id))],
            capture_output=True, text=True, timeout=10,
        )
        pids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def use_subprocess_worker() -> bool:
    """Whether the subprocess worker mode is enabled (default True)."""
    try:
        from config.settings import USE_SUBPROCESS_WORKER
        return bool(USE_SUBPROCESS_WORKER)
    except Exception:
        return True


def systemd_kill_mode(unit: str = "autotube-panel") -> Optional[str]:
    """KillMode of the systemd unit, or None if it cannot be determined."""
    for cmd in (
        ["systemctl", "show", "-p", "KillMode", unit],
        ["systemctl", "show", "-p", "KillMode", f"{unit}.service"],
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and "=" in out.stdout:
                return out.stdout.strip().split("=", 1)[1] or None
        except Exception:
            continue
    return None


def classify_jobs(
    job_ids: Iterable[int],
    *,
    use_subprocess: bool = True,
    pid_fn: Callable[[int], Optional[int]] = worker_pid,
) -> dict[int, str]:
    """Map each job id to ``'subprocess'`` or ``'in_process'``."""
    modes: dict[int, str] = {}
    for jid in job_ids:
        jid = int(jid)
        if not use_subprocess:
            modes[jid] = "in_process"
        else:
            modes[jid] = "subprocess" if pid_fn(jid) else "in_process"
    return modes


def decide(
    *,
    job_ids: Iterable[int],
    use_subprocess: bool = True,
    kill_mode: Optional[str],
    force: bool = False,
    isatty: bool = False,
    confirm: Optional[Callable[[], str]] = None,
    pid_fn: Callable[[int], Optional[int]] = worker_pid,
) -> tuple[str, str]:
    """Return ``(decision, reason)`` with decision in ``{'proceed', 'abort'}``."""
    job_ids = [int(j) for j in job_ids]
    if not job_ids:
        return "proceed", "no active long-form generation"

    modes = classify_jobs(job_ids, use_subprocess=use_subprocess, pid_fn=pid_fn)
    in_process = sorted(j for j, m in modes.items() if m == "in_process")

    if force:
        return "proceed", "forced by SKIP_ACTIVE_WORKER_CHECK"

    if in_process:
        if isatty and confirm is not None:
            try:
                answer = str(confirm()).strip().lower()
            except Exception:
                answer = ""
            if answer in ("y", "yes"):
                return "proceed", f"operator confirmed restart of in-process job(s) {in_process}"
        return "abort", f"in-process generation active for job(s) {in_process}"

    # All running jobs are subprocess workers. They survive the restart only if
    # systemd does not kill the whole cgroup. If KillMode is known and wrong,
    # refuse; if it is unknown (e.g. not running under systemd), allow — the
    # worker still has its own session and survives a SIGTERM to uvicorn.
    if kill_mode is not None and kill_mode != "process":
        return "abort", f"all workers subprocess but systemd KillMode={kill_mode} (need 'process')"

    return "proceed", f"all active workers run as subprocess {sorted(modes)} (survive restart)"


def _emit_skip_alert(reason: str) -> None:
    """Surface a skipped auto-deploy (best-effort, never raises)."""
    try:
        from database.db_extended import ExtendedDatabase
        from api.services.lifecycle_monitor import create_alert

        create_alert(
            ExtendedDatabase(),
            entity_type="system", entity_id=0, channel_id=None,
            alert_type="deploy_skipped", severity="warning",
            title="Deploy omitido: generación in-process activa",
            message=(
                f"apply_changes.sh NO reinició el API: {reason}.\n"
                "El código nuevo NO está en producción. Reintenta cuando termine "
                "la generación, o fuerza con SKIP_ACTIVE_WORKER_CHECK=true."
            ),
            metadata={"reason": reason},
        )
    except Exception:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    force = os.environ.get("SKIP_ACTIVE_WORKER_CHECK", "false").strip().lower() == "true"
    isatty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    jobs = running_longform_jobs()
    use_sub = use_subprocess_worker()
    kill_mode = systemd_kill_mode() if jobs and use_sub else None

    decision, reason = decide(
        job_ids=jobs,
        use_subprocess=use_sub,
        kill_mode=kill_mode,
        force=force,
        isatty=isatty,
        confirm=(lambda: input("Continue anyway? This WILL kill the running job [y/N]: "))
                if isatty else None,
        pid_fn=worker_pid,  # resolved at call time (monkeypatch-friendly)
    )

    if decision == "proceed":
        print(f"DEPLOY_SAFE: {reason}")
        return 0

    print(f"DEPLOY_BLOCKED: {reason}")
    _emit_skip_alert(reason)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
