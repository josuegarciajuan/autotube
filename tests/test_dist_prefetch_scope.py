"""Blinda dos correcciones del pipeline distribuido de imágenes IA (SuperServer):

1. `_prefetch_ai_images` solo pre-genera en la flota las escenas cuyo primer
   tier es IA (antes generaba 1 imagen por CADA escena → ~2 h de flota).
2. `_wait_dist_stall_aware` cancela y cae a local aunque el motor nunca publique
   `results/<eid>.json` (antes solo el ceiling completo, hasta 25 h).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _Sentinel(Exception):
    """Corta la ejecución justo después de `dsl_client.submit`."""


def test_prefetch_scope_usa_tiers(monkeypatch):
    import pipeline_dist.orchestrator_dist as od

    monkeypatch.setenv("AUTOTUBE_DIST_IMAGES", "1")
    captured: dict = {}

    class FakeMF:
        _local_sd = object()

        def _classify_scenes(self, scenes):
            # 4 escenas: 0 video, 1 stock-image, 2-3 ai_image
            types = {
                0: "video_priority",
                1: "stock_image_priority",
                2: "ai_image",
                3: "ai_image",
            }
            return {0}, {1}, types

    class FakeSelf:
        canal = "canalX"
        media_fetcher = FakeMF()

        def _prefetch_ai_images_dist(self, scene_ranges, script, only_indices=None):
            captured["indices"] = only_indices
            return {"ok": True}

    scene_ranges = [{"tipo": "desarrollo", "duration": 5} for _ in range(4)]
    result = od.DistributedOrchestrator._prefetch_ai_images(
        FakeSelf(), [], scene_ranges, None,
    )
    assert captured["indices"] == {2, 3}, "solo deben pre-generarse las AI-tier"
    assert result == {"ok": True}


def test_prefetch_scope_sin_escenas_ai_es_noop(monkeypatch):
    import pipeline_dist.orchestrator_dist as od

    monkeypatch.setenv("AUTOTUBE_DIST_IMAGES", "1")

    class FakeMF:
        _local_sd = object()

        def _classify_scenes(self, scenes):
            return {0}, {1}, {0: "video_priority", 1: "stock_image_priority"}

    class FakeSelf:
        canal = "canalX"
        media_fetcher = FakeMF()

        def _prefetch_ai_images_dist(self, *a, **k):  # no debe llamarse
            raise AssertionError("no debe lanzar job sin escenas AI-tier")

    scene_ranges = [{"tipo": "desarrollo", "duration": 5} for _ in range(2)]
    assert od.DistributedOrchestrator._prefetch_ai_images(
        FakeSelf(), [], scene_ranges, None,
    ) is None


def test_prefetch_dist_preserva_indice_global(monkeypatch, tmp_path):
    import pipeline_dist.orchestrator_dist as od

    monkeypatch.setattr(od.settings_mod, "OUTPUT_DIR", str(tmp_path))
    captured: dict = {}

    def fake_submit(definition, params, **kwargs):
        captured["definition"] = definition
        captured["params"] = params
        return "eid-test"

    monkeypatch.setattr(od.dsl_client, "submit", fake_submit)

    class FakeMF:
        _local_sd = object()

        def _build_ai_request(self, scene, i, n):
            return (f"prompt-{i}-of-{n}", "neg", None)

    class FakeSelf:
        canal = "canalX"
        db_video_id = 1
        media_fetcher = FakeMF()

        def _emit_progress(self, *a, **k):
            pass

        def _wait_dist_stall_aware(self, eid, timeout, progress_cb=None, stall_sec=900.0):
            raise _Sentinel()

    scene_ranges = [{"tipo": "desarrollo", "duration": 5} for _ in range(5)]
    with pytest.raises(_Sentinel):
        od.DistributedOrchestrator._prefetch_ai_images_dist(
            FakeSelf(), scene_ranges, {}, only_indices={1, 3},
        )
    keys = [im["key"] for im in captured["params"]["images"]]
    assert keys == ["0001", "0003"], "el índice global debe preservarse (hash 1:1)"
    assert captured["definition"] == "autotube-ai-image"


def test_stall_guard_cancela_sin_resultado(monkeypatch):
    import pipeline_dist.orchestrator_dist as od

    calls = {"cancel": 0}

    def fake_status(eid):
        return None  # el motor NUNCA publica resultados

    def fake_cancel(eid):
        calls["cancel"] += 1

    monkeypatch.setattr(od.dsl_client, "status", fake_status)
    monkeypatch.setattr(od.dsl_client, "cancel", fake_cancel)
    monkeypatch.setattr(od.time, "sleep", lambda _s: None)

    class Dummy:
        pass

    with pytest.raises(od.ScenePlanError) as exc:
        od.DistributedOrchestrator._wait_dist_stall_aware(
            Dummy(), "eid", timeout=100000, progress_cb=None, stall_sec=1,
        )
    assert calls["cancel"] == 1, "debe cancelar la ejecución estancada"
    assert "sin progreso" in str(exc.value)
