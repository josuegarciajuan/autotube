"""Optional job-scoped allowlist for the P04/P05 opt-in stack.

Production defaults stay OFF (``P04_SOURCE_ROUTER=0``, ``P05_INTEGRATED=0``).
This module adds a *narrowing* guard so an operator can enable the stack in a
shared service for a single job without opening it to every channel/job.

Contract
--------
* ``P05_CANARY_JOB_IDS`` unset/empty -> no narrowing (backwards compatible).
* ``P05_CANARY_JOB_IDS="15374,15375"`` -> the stack is only enabled when
  ``AUTOTUBE_JOB_ID`` matches one of the listed ids.

The API injects ``AUTOTUBE_JOB_ID`` into every worker subprocess, so a canary
can be scoped with::

    P04_SOURCE_ROUTER=1
    P05_INTEGRATED=1
    P05_CANARY_JOB_IDS=15374

and every other job of the same service stays on the legacy path.
"""

from __future__ import annotations

import os
from typing import Mapping


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def canary_job_scope(env: Mapping[str, str] | None = None) -> set[str]:
    """Return the configured job allowlist (empty means "not scoped")."""
    raw = str(_env(env).get("P05_CANARY_JOB_IDS", "") or "").strip()
    if not raw:
        return set()
    return {token.strip() for token in raw.split(",") if token.strip()}


def canary_scope_allows(env: Mapping[str, str] | None = None) -> bool:
    """True when the current process/job is allowed to run P04/P05."""
    allowed = canary_job_scope(env)
    if not allowed:
        return True
    current = str(_env(env).get("AUTOTUBE_JOB_ID", "") or "").strip()
    return bool(current) and current in allowed


def scope_description(env: Mapping[str, str] | None = None) -> str:
    """Human-readable scope label for logs/evidence (never prints secrets)."""
    allowed = canary_job_scope(env)
    if not allowed:
        return "unscoped"
    current = str(_env(env).get("AUTOTUBE_JOB_ID", "") or "").strip() or "-"
    return f"job:{current} in [{','.join(sorted(allowed))}]"
