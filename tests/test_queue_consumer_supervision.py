"""Regresión: la cola de generación no depende del cuerpo del scheduler.

Incidente 2026-10-26: el heartbeat de `queue_consumer` quedó congelado ~20 h
mientras el keepalive mantenía fresco el de `schedule_checker`. El job 15406
quedó `queued` sin despachar. Estos tests fijan las dos piezas del arreglo:

1. `_queue_consumer_loop` es un loop propio que invoca el consumidor.
2. El keepalive del scheduler deja de latir si el cuerpo se cuelga, para que
   el task_watchdog pueda reiniciar la API.
3. El scheduler ya no invoca `_queue_consumer()` directamente.
"""

import asyncio
import inspect

import pytest

import api.main as main


def test_scheduler_iteration_stalled():
    assert main._scheduler_iteration_stalled(10, 7200) is False
    assert main._scheduler_iteration_stalled(7201, 7200) is True
    # ceiling <= 0 desactiva la detección (kill-switch)
    assert main._scheduler_iteration_stalled(10 ** 9, 0) is False


@pytest.mark.asyncio
async def test_queue_consumer_loop_invokes_consumer(monkeypatch):
    calls = []

    async def fake_consumer():
        calls.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "_queue_consumer", fake_consumer)
    monkeypatch.setattr(
        "api.services.lifecycle_monitor.touch_task_heartbeat",
        lambda *a, **k: None,
    )

    with pytest.raises(asyncio.CancelledError):
        await main._queue_consumer_loop(startup_delay=0, interval=0)

    assert calls == [1]


def test_scheduler_loop_no_longer_calls_queue_consumer():
    source = inspect.getsource(main._schedule_checker_loop)
    assert "await _queue_consumer()" not in source
