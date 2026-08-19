"""RAM Governor — proactive memory management for video generation pipeline.

Prevents OOM crashes and BrokenPipe errors by:
1. Checking available RAM before heavy phases
2. Adaptively scaling ffmpeg threads based on available RAM
3. Providing a wait_for_ram() gate for sequential job processing

All functions are non-blocking and safe to call from any thread.
"""

import os
import time
import logging

from config.settings import MIN_FREE_FOR_RENDER_MB, MIN_FREE_FOR_DISPATCH_MB

logger = logging.getLogger("autotube.ram_governor")

# Thresholds in MB (from settings / env vars)
MIN_FREE_FOR_RENDER = MIN_FREE_FOR_RENDER_MB  # Minimum free RAM before starting a render
MIN_FREE_FOR_DISPATCH = MIN_FREE_FOR_DISPATCH_MB  # Minimum free RAM before dispatching a new job


def _swap_pressure_factor() -> float:
    """Discount factor (0.0-1.0) reflecting how full swap is.

    Under sustained memory pressure the kernel fills swap; MemAvailable still
    reports reclaimable page cache as "available", but reclaiming it causes
    swap thrash and OOM kills mid-render/TTS. This factor reduces the
    effective available RAM as swap fills: no discount below 60% usage,
    scaling linearly down to a 0.5 factor at 100% usage.
    """
    try:
        swap_total = 0
        swap_free = 0
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("SwapTotal:"):
                    swap_total = int(line.split()[1])
                elif line.startswith("SwapFree:"):
                    swap_free = int(line.split()[1])
                if swap_total and swap_free:
                    break
        if swap_total <= 0:
            return 1.0
        used_ratio = (swap_total - swap_free) / float(swap_total)
        if used_ratio <= 0.6:
            return 1.0
        return max(0.5, 1.0 - 0.5 * (used_ratio - 0.6) / 0.4)
    except Exception:
        return 1.0


def available_mb() -> int:
    """Return available physical memory in MB, or -1 if unavailable.

    Uses /proc/meminfo MemAvailable (includes reclaimable page cache) for
    accuracy, with sysconf fallback.  SC_AVPHYS_PAGES alone underreports
    available memory by 5-10 GB on typical Linux systems.

    v (Aug 2026): the MemAvailable figure is discounted by swap pressure —
    when swap is nearly full, reclaimable cache is not truly "free" without
    thrashing, so the effective available RAM is lower than MemAvailable alone.
    """
    try:
        # Primary: /proc/meminfo (includes reclaimable cache)
        mem_available = None
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    mem_available = kb // 1024
                    break
        if mem_available is None:
            # Fallback: sysconf (legacy, known to underreport)
            avail_bytes = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            return avail_bytes // (1024 * 1024)
        factor = _swap_pressure_factor()
        return int(mem_available * factor)
    except Exception:
        return -1


def wait_for_ram(min_mb: int = MIN_FREE_FOR_RENDER, timeout_sec: int = 600) -> bool:
    """Block until at least ``min_mb`` RAM is free, or timeout.

    Returns:
        True if enough RAM is available, False on timeout.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        avail = available_mb()
        if avail < 0 or avail >= min_mb:
            return avail >= min_mb
        logger.info(
            "RAM governor: %d MB free < %d MB needed — waiting 30s", avail, min_mb
        )
        time.sleep(30)
    logger.warning(
        "RAM governor: timeout after %ds waiting for %d MB (currently %d MB)",
        timeout_sec,
        min_mb,
        available_mb(),
    )
    return False


def recommended_ffmpeg_threads() -> int:
    """Return the recommended number of ffmpeg encoder threads based on free RAM.

    Scales down only when memory is critically low. On modern servers with
    8+ cores, 4 threads with -preset fast achieves good throughput without
    exhausting RAM.

    Returns:
        Thread count: 2 (critical) → 4 (normal) → min(6, cpu_count) (plenty).
    """
    import os as _os
    avail = available_mb()
    if avail < 0:
        return min(6, _os.cpu_count() or 4)  # Unknown — be optimistic
    if avail < 2000:
        return 2
    if avail < 4000:
        return 3
    if avail < 6000:
        return 4
    return min(6, _os.cpu_count() or 6)


def is_ram_ok_for_render() -> bool:
    """Check if there's enough RAM to safely start a video render."""
    avail = available_mb()
    if avail < 0:
        return True  # Can't determine — let it proceed
    return avail >= MIN_FREE_FOR_RENDER


def is_ram_ok_for_dispatch() -> bool:
    """Check if there's enough RAM to safely dispatch a new generation job."""
    avail = available_mb()
    if avail < 0:
        return True  # Can't determine — let it proceed
    return avail >= MIN_FREE_FOR_DISPATCH
