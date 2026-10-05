"""Test de la guardia de estancamiento del orquestador distribuido.

Regresión: un worker colgado deja ``inflight>0`` indefinidamente; antes eso se
interpretaba como "progreso vivo" y podía colgar el vídeo hasta el timeout
completo. Ahora el latido es la salida real (accepted/generated).
"""
from __future__ import annotations

import pytest

from pipeline_dist import orchestrator_dist as orch
from pipeline_dist.scene_plan import ScenePlanError


class _FakeTime:
    def __init__(self, step: float = 100.0):
        self.t = 0.0
        self.step = step

    def time(self) -> float:
        self.t += self.step
        return self.t

    def sleep(self, _sec: float) -> None:  # noqa: D401
        return None


def _call(eid="exec-1", timeout=1e9, stall_sec=300.0, progress_cb=None):
    # El método no usa `self`; basta un objeto cualquiera.
    return orch.DistributedOrchestrator._wait_dist_stall_aware(
        object(), eid, timeout, progress_cb=progress_cb, stall_sec=stall_sec,
    )


def test_stuck_inflight_cancels(monkeypatch):
    cancelled: list[str] = []
    monkeypatch.setattr(orch, "time", _FakeTime())
    monkeypatch.setattr(
        orch.dsl_client, "status",
        lambda eid: {"status": "running",
                     "progress": {"inflight": 6, "accepted": 0,
                                  "generated": 0, "pending": 0}},
    )
    monkeypatch.setattr(orch.dsl_client, "cancel", lambda eid: cancelled.append(eid))

    with pytest.raises(ScenePlanError):
        _call(stall_sec=300.0)
    assert cancelled == ["exec-1"]


def test_real_progress_keeps_alive_and_returns(monkeypatch):
    state = {"n": 0}
    cancelled: list[str] = []
    monkeypatch.setattr(orch, "time", _FakeTime(step=10.0))

    def _status(eid):
        state["n"] += 1
        if state["n"] >= 8:
            return {"status": "done", "progress": {}}
        # accepted crece en cada tick → hay progreso real.
        return {"status": "running",
                "progress": {"inflight": 3, "accepted": state["n"],
                             "generated": state["n"], "pending": 1}}

    monkeypatch.setattr(orch.dsl_client, "status", _status)
    monkeypatch.setattr(orch.dsl_client, "cancel", lambda eid: cancelled.append(eid))

    out = _call(stall_sec=300.0)
    assert out["status"] == "done"
    assert cancelled == []
