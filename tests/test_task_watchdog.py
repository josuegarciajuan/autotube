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
