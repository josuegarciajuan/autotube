"""Tests for the background-loop watchdog."""

import asyncio

import pytest

from api.services.task_watchdog import supervise_loop


@pytest.mark.asyncio
async def test_supervisor_restarts_failed_loop_with_bounded_attempts():
    attempts = 0
    stopped = asyncio.Event()

    async def loop_factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient loop failure")
        stopped.set()
        await asyncio.Event().wait()

    supervisor = asyncio.create_task(
        supervise_loop(
            "test_loop",
            loop_factory,
            restart_delay=0,
            max_restarts=2,
            monitor_interval=0.001,
            startup_grace=0,
        )
    )
    await asyncio.wait_for(stopped.wait(), timeout=1)
    assert attempts == 2
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor


@pytest.mark.asyncio
async def test_supervisor_does_not_restart_after_restart_budget_is_exhausted():
    attempts = 0

    async def loop_factory():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("permanent loop failure")

    await asyncio.wait_for(
        supervise_loop(
            "test_loop",
            loop_factory,
            restart_delay=0,
            max_restarts=2,
            monitor_interval=0.001,
            startup_grace=0,
        ),
        timeout=1,
    )
    assert attempts == 3


@pytest.mark.asyncio
async def test_supervisor_calls_stale_recovery_without_restarting_task(monkeypatch):
    import api.services.lifecycle_monitor as lifecycle_monitor
    import api.services.task_watchdog as watchdog

    recovery = []
    started = asyncio.Event()

    monkeypatch.setattr(watchdog, "get_task_heartbeat_age", lambda _: 999.0)
    monkeypatch.setattr(lifecycle_monitor, "task_is_stale", lambda _: True)

    async def loop_factory():
        started.set()
        await asyncio.Event().wait()

    await asyncio.wait_for(
        watchdog.supervise_loop(
            "stale_loop",
            loop_factory,
            monitor_interval=0.001,
            startup_grace=0,
            cancel_grace=0.1,
            on_stale=lambda name: recovery.append(name),
        ),
        timeout=1,
    )

    assert started.is_set()
    assert recovery == ["stale_loop"]
