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

# Gracia para loops que nunca llegan a emitir su primer heartbeat in-process.
# Algunos loops supervisados duermen ANTES de su primer touch (el máximo es
# resume_phase: 300s en api/main.py). Debe superar ese máximo con margen para
# no matar un loop sano en su arranque legítimo. Un loop que sigue sin latir
# tras este umbral está bloqueado antes de su primer heartbeat y debe reiniciarse.
NEVER_HEARTBEAT_GRACE = 600.0


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
    on_stale: Callable[[str], None] | None = None,
) -> None:
    """Run and cautiously restart one background loop.

    ``max_restarts`` is a rolling budget, preventing a broken loop from
    becoming a hot restart loop. A stale task may invoke ``on_stale`` for
    process-level recovery; it must not be restarted in-process because a
    cancelled task waiting on ``asyncio.to_thread`` can leave its thread alive.
    """
    restart_times: list[float] = []
    child: asyncio.Task[None] | None = None
    started_at = time.monotonic()

    try:
        while True:
            child = asyncio.create_task(factory(), name=f"loop:{task_name}")
            # Reinicia la bandera por encarnación: si el loop nunca llega a
            # emitir su heartbeat in-process (bloqueado en su setup), la
            # detección de staleness lo captura igualmente (age is None).
            seen_heartbeat = False
            logger.info("Background loop '%s' (re)started", task_name)
            try:
                while not child.done():
                    await asyncio.sleep(monitor_interval)
                    age = get_task_heartbeat_age(task_name)
                    if age is not None:
                        seen_heartbeat = True
                    elapsed_since_start = time.monotonic() - started_at
                    from api.services.lifecycle_monitor import task_is_stale
                    # 1) Heartbeat conocido pero superado su timeout (loop
                    #    colgado a mitad de iteración).
                    stale_timeout = (
                        age is not None
                        and elapsed_since_start >= startup_grace
                        and task_is_stale(task_name)
                    )
                    # 2) Loop que nunca emitió heartbeat in-process: bloqueado
                    #    antes de latir. NEVER_HEARTBEAT_GRACE > max sleep
                    #    inicial (300s) para no matar arranques legítimos.
                    never_heartbeat = (
                        elapsed_since_start >= NEVER_HEARTBEAT_GRACE
                        and not seen_heartbeat
                    )
                    if stale_timeout or never_heartbeat:
                        logger.error(
                            "Background loop '%s' heartbeat is stale%s; cancelling safely",
                            task_name,
                            " (never touched)" if never_heartbeat else "",
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
                        if on_stale is not None:
                            try:
                                on_stale(task_name)
                            except Exception:
                                logger.exception(
                                    "Background loop '%s' stale recovery callback failed",
                                    task_name,
                                )
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
