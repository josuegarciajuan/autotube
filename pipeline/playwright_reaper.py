"""Reaper for orphaned Playwright Node driver processes.

Why this exists
---------------
``playwright.sync_api.sync_playwright().start()`` spawns a Node child process
(``node .../playwright/driver/package/cli.js run-driver``) glued to the thread
that started it. If that thread exits — or if ``pw.stop()`` is attempted from a
different thread (impossible with the sync API) — the Node driver survives as a
child of the API process. Repeated over hours this accumulates dozens of
drivers (~4.2 GB RSS observed in production).

The primary fix lives in ``pipeline/youtube_browser.py`` (owner-aware lifecycle
+ registry). This module is the **safety net**: it periodically SIGKILLs ONLY
``run-driver`` direct children of THIS process whose age exceeds a generous TTL
(default 1h). It never touches:

  * drivers of other PIDs (workers, the egress agent, dev machines),
  * recent drivers (< TTL), which may belong to an in-flight job,
  * non-driver processes.

Kill-switch: ``PLAYWRIGHT_REAPER_ENABLED=false``.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("autotube.playwright_reaper")

# ── Configuration (env, all read at call time so tests can monkeypatch) ──
DEFAULT_TTL_SECONDS = 3600          # > 1 h: never touch in-flight jobs (~20 min)
DEFAULT_INTERVAL_SECONDS = 600      # 10 min
DEFAULT_ORPHAN_GRACE_SECONDS = 120  # a driver whose owner thread is gone may
                                    # be reaped sooner than the full TTL

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_boot_time_cache: float | None = None

# ── Metrics ──
_stats_lock = threading.Lock()
_last_stats: dict = {
    "enabled": True,
    "runs": 0,
    "scanned": 0,
    "killed": 0,
    "killed_pids": [],
    "last_run_epoch": 0.0,
    "last_error": "",
}
_total_killed = 0


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "disabled", "")


def is_enabled() -> bool:
    """Kill-switch. Default ON."""
    return _env_bool("PLAYWRIGHT_REAPER_ENABLED", True)


def ttl_seconds() -> float:
    try:
        return float(os.getenv("PLAYWRIGHT_DRIVER_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        return float(DEFAULT_TTL_SECONDS)


def interval_seconds() -> float:
    try:
        return float(os.getenv("PLAYWRIGHT_REAPER_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        return float(DEFAULT_INTERVAL_SECONDS)


def orphan_grace_seconds() -> float:
    try:
        return float(os.getenv("PLAYWRIGHT_REAPER_ORPHAN_GRACE_SECONDS",
                               DEFAULT_ORPHAN_GRACE_SECONDS))
    except (TypeError, ValueError):
        return float(DEFAULT_ORPHAN_GRACE_SECONDS)


# ── /proc helpers (no fragile CLI utilities) ────────────────────────

def _iter_child_pids(pid: int) -> set:
    """Direct child PIDs of ``pid`` via /proc/<pid>/task/*/children (union).

    Children are tracked per-task (thread), so we union every task's children.
    Falls back to ``ps --ppid`` when /proc is unavailable (containers).
    """
    pids: set = set()
    task_dir = Path(f"/proc/{pid}/task")
    try:
        for task in task_dir.iterdir():
            try:
                raw = (task / "children").read_text()
            except OSError:
                continue
            for tok in raw.split():
                if tok.isdigit():
                    pids.add(int(tok))
    except OSError:
        pass
    if not pids:
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=", "--ppid", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for tok in out.split():
                if tok.isdigit():
                    pids.add(int(tok))
        except Exception:
            pass
    return pids


def _read_cmdline(pid: int) -> list:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.split(b"\x00") if part]


def _boot_time() -> float:
    global _boot_time_cache
    if _boot_time_cache is not None:
        return _boot_time_cache
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                _boot_time_cache = float(line.split()[1])
                return _boot_time_cache
    except OSError:
        pass
    return time.time()


def _process_start_epoch(pid: int) -> float | None:
    """Process start time as epoch seconds (from /proc/<pid>/stat field 22)."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rp = data.rfind(")")
    if rp < 0:
        return None
    fields = data[rp + 2:].split()
    if len(fields) < 20:
        return None
    try:
        start_ticks = int(fields[19])  # field 22 - first 3 fields = index 19
    except ValueError:
        return None
    return _boot_time() + start_ticks / _CLK_TCK


def _process_age_seconds(pid: int) -> float | None:
    started = _process_start_epoch(pid)
    if started is None:
        return None
    return max(0.0, time.time() - started)


def _parent_pid(pid: int) -> int | None:
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rp = data.rfind(")")
    if rp < 0:
        return None
    fields = data[rp + 2:].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])  # field 4 (ppid) - first 3 = index 1
    except ValueError:
        return None


def _is_driver_cmdline(cmdline: list) -> bool:
    """True if this argv is a Playwright Node driver.

    Playwright always invokes the driver as ``node .../cli.js run-driver``.
    Matching the ``run-driver`` token (exact or as a path suffix) is strict
    enough to avoid false positives while surviving driver path changes.
    """
    for arg in cmdline:
        text = arg.decode("utf-8", "replace") if isinstance(arg, bytes) else str(arg)
        if text == "run-driver" or text.endswith("/run-driver"):
            return True
    return False


def list_driver_children(parent_pid: int | None = None) -> list:
    """Return driver children of ``parent_pid`` (default: this process).

    Each item: ``{"pid", "ppid", "age_seconds", "cmdline"}``. Only direct
    children of ``parent_pid`` are ever returned — never other processes.
    """
    parent = os.getpid() if parent_pid is None else parent_pid
    out = []
    for pid in _iter_child_pids(parent):
        if _parent_pid(pid) != parent:
            continue
        cmdline = _read_cmdline(pid)
        if not _is_driver_cmdline(cmdline):
            continue
        out.append({
            "pid": pid,
            "ppid": parent,
            "age_seconds": _process_age_seconds(pid),
            "cmdline": [c.decode("utf-8", "replace") if isinstance(c, bytes) else c
                        for c in cmdline],
        })
    return out


# ── Kill ────────────────────────────────────────────────────────────

def _kill_pid(pid: int, parent_pid: int | None = None) -> bool:
    """SIGKILL ``pid`` after re-verifying it is still a driver child.

    Re-checking closes the PID-reuse window: we never kill a process that is
    no longer a ``run-driver`` child of the expected parent.
    """
    parent = os.getpid() if parent_pid is None else parent_pid
    if _parent_pid(pid) != parent or not _is_driver_cmdline(_read_cmdline(pid)):
        return False
    try:
        os.kill(pid, signal.SIGKILL)
        logger.warning("Reaped orphaned Playwright driver pid=%s (parent=%s)", pid, parent)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        logger.debug("Could not kill driver pid=%s: %s", pid, exc)
        return False


def kill_all_driver_children(parent_pid: int | None = None) -> list:
    """Kill EVERY driver child of this process (used at process exit).

    At atexit the process is leaving, so no driver child can be legitimately
    in use. Returns the list of killed PIDs.
    """
    parent = os.getpid() if parent_pid is None else parent_pid
    killed = []
    for child in list_driver_children(parent):
        if _kill_pid(child["pid"], parent):
            killed.append(child["pid"])
    return killed


# ── Registry-backed reaping ─────────────────────────────────────────

def _registry_snapshot() -> list:
    try:
        from pipeline.youtube_browser import get_playwright_registry_snapshot
        return get_playwright_registry_snapshot()
    except Exception:
        return []


def _owner_thread_alive(meta: dict) -> bool:
    ident = meta.get("owner_ident")
    if ident is None:
        return False
    try:
        return any(t.ident == ident for t in threading.enumerate() if t.ident is not None)
    except Exception:
        return True


# ── Main pass ───────────────────────────────────────────────────────

def reap_once(ttl: float | None = None,
              parent_pid: int | None = None,
              children: list | None = None,
              registry_entries: list | None = None,
              orphan_grace: float | None = None) -> dict:
    """One reaping pass. Safe to call from any thread.

    Injectable ``children`` / ``registry_entries`` make it unit-testable without
    spawning real processes.
    """
    parent = os.getpid() if parent_pid is None else parent_pid
    effective_ttl = ttl if ttl is not None else ttl_seconds()
    grace = orphan_grace if orphan_grace is not None else orphan_grace_seconds()

    stats = {
        "enabled": is_enabled(),
        "parent_pid": parent,
        "ttl_seconds": effective_ttl,
        "scanned": 0,
        "killed": 0,
        "killed_pids": [],
        "skipped_recent": 0,
        "orphaned_registry": 0,
        "orphan_killed_pids": [],
        "errors": [],
    }
    if not stats["enabled"]:
        return stats

    if children is None:
        try:
            children = list_driver_children(parent)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"list_driver_children: {exc}")
            children = []

    for child in children or []:
        pid = child.get("pid")
        age = child.get("age_seconds")
        if pid is None:
            continue
        stats["scanned"] += 1
        if age is None or age < effective_ttl:
            stats["skipped_recent"] += 1
            continue
        if _kill_pid(pid, parent):
            stats["killed"] += 1
            stats["killed_pids"].append(pid)

    # Registry entries whose owner thread is gone (or that were explicitly
    # marked orphaned) are definitely not in use: reap their driver PID after a
    # short grace, even before the full TTL.
    if registry_entries is None:
        try:
            registry_entries = _registry_snapshot()
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"registry_snapshot: {exc}")
            registry_entries = []
    for meta in registry_entries or []:
        driver_pid = meta.get("driver_pid")
        if not driver_pid:
            continue
        orphaned = bool(meta.get("orphaned")) or bool(meta.get("stop_requested"))
        if not orphaned and _owner_thread_alive(meta):
            continue
        stats["orphaned_registry"] += 1
        age = _process_age_seconds(driver_pid)
        if age is None or age < grace:
            continue
        if _kill_pid(driver_pid, parent):
            stats["orphan_killed_pids"].append(driver_pid)

    return stats


def reap_if_enabled() -> dict:
    """Run one pass, recording metrics. Never raises."""
    global _total_killed
    if not is_enabled():
        return {"enabled": False, "runs": _last_stats.get("runs", 0)}
    try:
        stats = reap_once()
    except Exception as exc:  # noqa: BLE001
        logger.warning("playwright reaper pass failed: %s", exc)
        with _stats_lock:
            _last_stats["last_error"] = str(exc)
            _last_stats["runs"] = _last_stats.get("runs", 0) + 1
        return {"enabled": True, "error": str(exc)}
    with _stats_lock:
        _last_stats["enabled"] = True
        _last_stats["runs"] = _last_stats.get("runs", 0) + 1
        _last_stats["scanned"] = stats.get("scanned", 0)
        _last_stats["killed"] = stats.get("killed", 0) + len(stats.get("orphan_killed_pids", []))
        _last_stats["killed_pids"] = (
            stats.get("killed_pids", []) + stats.get("orphan_killed_pids", [])
        )
        _last_stats["last_run_epoch"] = time.time()
        _last_stats["last_error"] = ""
        _total_killed += _last_stats["killed"]
    return stats


def get_reaper_stats() -> dict:
    """Snapshot of counters for endpoints/logging."""
    with _stats_lock:
        snap = dict(_last_stats)
    snap["total_killed"] = _total_killed
    snap["enabled"] = is_enabled()
    snap["ttl_seconds"] = ttl_seconds()
    snap["interval_seconds"] = interval_seconds()
    try:
        snap["current_driver_children"] = list_driver_children()
    except Exception:
        snap["current_driver_children"] = []
    return snap


def start_reaper_thread(stop_event: threading.Event | None = None,
                        interval: float | None = None) -> threading.Thread:
    """Optional standalone daemon thread (not used by the API, which drives the
    reaper from an asyncio loop). Provided for scripts/standalone processes."""
    stop_event = stop_event or threading.Event()
    period = interval if interval is not None else interval_seconds()

    def _run():
        while not stop_event.is_set():
            try:
                reap_if_enabled()
            except Exception:  # noqa: BLE001
                pass
            stop_event.wait(period)

    t = threading.Thread(target=_run, name="playwright-reaper", daemon=True)
    t.start()
    return t
