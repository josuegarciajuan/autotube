"""Tests del relanzamiento del exec distribuido (resiliencia Fase 2, opción B).

El motor falla el exec entero si una unidad agota reintentos y no publica
unidades parciales, así que ante incompleto/estancamiento se relanza el exec
COMPLETO con id nuevo (el scheduler reasigna nodo) antes del fallback local.
"""

import pytest

from pipeline_dist.orchestrator_dist import (
    DistributedOrchestrator,
    ScenePlanError,
    _dist_repair_rounds,
)


def test_dist_repair_rounds_env(monkeypatch):
    monkeypatch.delenv("AUTOTUBE_DIST_REPAIR", raising=False)
    monkeypatch.delenv("AUTOTUBE_DIST_REPAIR_ROUNDS", raising=False)
    assert _dist_repair_rounds() == 2  # default

    monkeypatch.setenv("AUTOTUBE_DIST_REPAIR_ROUNDS", "0")
    assert _dist_repair_rounds() == 0

    monkeypatch.setenv("AUTOTUBE_DIST_REPAIR", "false")
    monkeypatch.setenv("AUTOTUBE_DIST_REPAIR_ROUNDS", "5")
    assert _dist_repair_rounds() == 0  # kill-switch wins


def test_repair_relaunches_full_exec_until_complete():
    calls = {"submit": 0, "wait": 0, "obs": 0}

    def submit_fn(attempt):
        calls["submit"] += 1
        return f"eid-r{attempt}"

    def wait_fn(eid):
        calls["wait"] += 1
        # Falla las 2 primeras rondas; completa la 3ª.
        if calls["wait"] < 3:
            raise ScenePlanError("stall simulado")
        return {"status": "done", "items": [1, 2, 3]}

    summary = DistributedOrchestrator._dist_with_repair(
        submit_fn=submit_fn,
        wait_fn=wait_fn,
        is_complete=lambda s: s.get("status") == "done" and len(s["items"]) == 3,
        phase="render v2", canal="canalX", repair_rounds=2,
        obs_name="dist_render_repair",
    )
    assert summary["status"] == "done"
    assert calls["submit"] == 3 and calls["wait"] == 3


def test_repair_incomplete_result_triggers_retry():
    seq = iter([
        {"status": "done", "items": [1]},      # incompleto (engine falló unidad)
        {"status": "done", "items": [1, 2]},   # completo
    ])

    def wait_fn(eid):
        return next(seq)

    summary = DistributedOrchestrator._dist_with_repair(
        submit_fn=lambda a: f"e{a}",
        wait_fn=wait_fn,
        is_complete=lambda s: s.get("status") == "done" and len(s["items"]) == 2,
        phase="concat", canal="canalX", repair_rounds=2,
        obs_name="dist_concat_repair",
    )
    assert summary["items"] == [1, 2]


def test_repair_exhausted_raises_for_local_fallback():
    def wait_fn(eid):
        raise ScenePlanError("stall permanente")

    with pytest.raises(ScenePlanError):
        DistributedOrchestrator._dist_with_repair(
            submit_fn=lambda a: f"e{a}",
            wait_fn=wait_fn,
            is_complete=lambda s: False,
            phase="render v2", canal="canalX", repair_rounds=1,
            obs_name="dist_render_repair",
        )


def test_repair_disabled_single_attempt():
    calls = {"n": 0}

    def wait_fn(eid):
        calls["n"] += 1
        return {"status": "failed", "error": "boom"}

    with pytest.raises(ScenePlanError):
        DistributedOrchestrator._dist_with_repair(
            submit_fn=lambda a: f"e{a}",
            wait_fn=wait_fn,
            is_complete=lambda s: False,
            phase="render v2", canal="canalX", repair_rounds=0,
            obs_name="dist_render_repair",
        )
    assert calls["n"] == 1
