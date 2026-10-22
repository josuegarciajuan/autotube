"""Test de la guardia de estancamiento del orquestador distribuido.

Regresión: la guardia no debe cancelar mientras haya unidades inflight/pending
(pueden estar esperando capacidad y el motor ya gobierna su timeout). Solo
cancela si el motor no publica estado o si no queda trabajo en vuelo; el
timeout global de la ejecución sigue siendo el cinturón de seguridad.
"""
from __future__ import annotations

import types

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


def test_stuck_inflight_waits_for_overall_timeout(monkeypatch):
    """Un worker inflight puede estar esperando capacidad.

    La guardia de 300 s ya no lo mata: el motor gobierna el timeout por unidad
    y, si de verdad está colgado, se corta al timeout global de la ejecución.
    """
    cancelled: list[str] = []
    monkeypatch.setattr(orch, "time", _FakeTime())
    monkeypatch.setattr(
        orch.dsl_client, "status",
        lambda eid: {"status": "running",
                     "progress": {"inflight": 6, "accepted": 0,
                                  "generated": 0, "pending": 0}},
    )
    monkeypatch.setattr(orch.dsl_client, "cancel", lambda eid: cancelled.append(eid))

    with pytest.raises(ScenePlanError, match="timeout"):
        _call(timeout=1000.0, stall_sec=300.0)
    assert cancelled == ["exec-1"]


def test_no_status_cancels_by_stall(monkeypatch):
    """Si el motor nunca publica estado, sí se corta por estancamiento."""
    cancelled: list[str] = []
    monkeypatch.setattr(orch, "time", _FakeTime())
    monkeypatch.setattr(orch.dsl_client, "status", lambda eid: None)
    monkeypatch.setattr(orch.dsl_client, "cancel", lambda eid: cancelled.append(eid))

    with pytest.raises(ScenePlanError, match="sin progreso"):
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


# ── Wrappers que ahora usan la guardia (render v2 y concat) ──────────────────

def test_render_v2_stall_cancels(monkeypatch, tmp_path):
    """Render v2: si no hay progreso real, cancela y cae a local."""
    cancelled: list[str] = []
    monkeypatch.setattr(orch, "time", _FakeTime())
    monkeypatch.setattr(
        orch.dsl_client, "status",
        lambda eid: {"status": "running",
                     "progress": {"inflight": 4, "accepted": 0,
                                  "generated": 0, "pending": 0}},
    )
    monkeypatch.setattr(orch.dsl_client, "cancel", lambda eid: cancelled.append(eid))
    monkeypatch.setattr(orch.dsl_client, "submit", lambda *a, **k: "eid-r2")
    monkeypatch.setattr(orch.settings_mod, "VIDEOS_DIR", str(tmp_path))
    monkeypatch.setattr(
        orch, "build_scene_plans",
        lambda ve, sr, ma: [{"reproducible": True} for _ in sr],
    )

    asset = tmp_path / "asset.jpg"
    asset.write_bytes(b"jpg")
    scene_ranges = [{"start_ms": 0, "end_ms": 1000}]
    media_assets = [{"path": str(asset)}]

    class FakeSelf(orch.DistributedOrchestrator):
        def __init__(self):
            self.canal = "canalX"
            self.db_video_id = 7
            self.config = types.SimpleNamespace()
            self._last_scene_ranges = scene_ranges
            self._video_editor = types.SimpleNamespace(_video_seed=0)

        def _emit_progress(self, *a, **k):
            pass

    with pytest.raises(ScenePlanError):
        FakeSelf()._pre_render_scenes_v2(
            {"id": 1}, {"timestamps": []}, media_assets, 7,
        )
    assert set(cancelled) == {"eid-r2"}


def test_concat_stall_cancels(monkeypatch, tmp_path):
    """Concat distribuido: si no hay progreso real, cancela y cae a local."""
    cancelled: list[str] = []
    monkeypatch.setattr(orch, "time", _FakeTime())
    monkeypatch.setattr(
        orch.dsl_client, "status",
        lambda eid: {"status": "running",
                     "progress": {"inflight": 2, "accepted": 0,
                                  "generated": 0, "pending": 0}},
    )
    monkeypatch.setattr(orch.dsl_client, "cancel", lambda eid: cancelled.append(eid))
    monkeypatch.setattr(orch.dsl_client, "submit", lambda *a, **k: "eid-cc")

    segs = []
    for i in range(2):
        p = tmp_path / f"seg_{i}.mp4"
        p.write_bytes(b"mp4")
        segs.append(str(p))

    class FakeVE:
        canal: dict = {}

        def _build_duration_preserving_concat_filter(self, n, **kw):
            return ("", "v")

    class FakeSelf(orch.DistributedOrchestrator):
        def __init__(self):
            self.canal = "canalX"
            self.db_video_id = 7
            self._video_editor = FakeVE()

        def _emit_progress(self, *a, **k):
            pass

    with pytest.raises(ScenePlanError):
        FakeSelf()._dist_concat_body_batched(
            segs, [(0, 1), (1, 2)], str(tmp_path / "out.mp4"), batch_size=1,
        )
    assert set(cancelled) == {"eid-cc"}
