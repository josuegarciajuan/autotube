"""Supervise API background loops without creating duplicate work.

The watchdog only restarts a loop after its task has actually terminated.  A
stale heartbeat is reported and the task is cancelled, but a replacement is
created only once cancellation has completed.  This is deliberate: cancelling
an asyncio task waiting on a thread does not stop that thread and restarting it
would run two schedulers concurrently.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from api.services.lifecycle_monitor import get_task_heartbeat_age

logger = logging.getLogger("autotube.task_watchdog")


async def supervise_loop(
    task_name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    restart_delay: float = 5.0,
    max_restarts: int = 3,
    restart_window: float = 3600.0,
    monitor_interval: float = 30.0,
    startup_grace: float = 300.0,
    cancel_grace: float = 5.0,
) -> None:
    """Run and cautiously restart one background loop.

    ``max_restarts`` is a rolling budget, preventing a broken loop from
    becoming a hot restart loop.  Cancellation is bounded; if the child does
    not stop, supervision stops rather than starting a duplicate scheduler.
    """
    restart_times: list[float] = []
    child: asyncio.Task[None] | None = None
    started_at = time.monotonic()

    try:
        while True:
            child = asyncio.create_task(factory(), name=f"loop:{task_name}")
            try:
                while not child.done():
                    await asyncio.sleep(monitor_interval)
                    age = get_task_heartbeat_age(task_name)
                    if (
                        age is not None
                        and time.monotonic() - started_at >= startup_grace
                        and age > 0
                    ):
                        # The DB heartbeat contains wall-clock age; use the
                        # caller's timeout policy in lifecycle_monitor.
                        from api.services.lifecycle_monitor import task_is_stale
                        if task_is_stale(task_name):
                            logger.error(
                                "Background loop '%s' heartbeat is stale; cancelling safely",
                                task_name,
                            )
                            child.cancel()
                            try:
                                await asyncio.wait_for(child, timeout=cancel_grace)
                            except asyncio.CancelledError:
                                pass
                            except asyncio.TimeoutError:
                                logger.critical(
                                    "Background loop '%s' did not cancel; no restart to avoid duplicate work",
                                    task_name,
                                )
                                return
                            break

                if child.cancelled():
                    return
                exc = child.exception()
                if exc is None:
                    logger.warning("Background loop '%s' exited unexpectedly", task_name)
                else:
                    logger.error(
                        "Background loop '%s' crashed: %s",
                        task_name,
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            except asyncio.CancelledError:
                if child is not None and not child.done():
                    child.cancel()
                    try:
                        await asyncio.wait_for(child, timeout=cancel_grace)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                raise

            now = time.monotonic()
            restart_times = [t for t in restart_times if now - t < restart_window]
            if len(restart_times) >= max_restarts:
                logger.critical(
                    "Background loop '%s' exceeded restart budget (%d/%ss); disabled",
                    task_name, max_restarts, int(restart_window),
                )
                return
            restart_times.append(now)
            await asyncio.sleep(restart_delay * (2 ** (len(restart_times) - 1)))
            started_at = time.monotonic()
    finally:
        if child is not None and not child.done():
            child.cancel()
