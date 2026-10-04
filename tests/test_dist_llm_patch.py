"""Tests del parche de estabilidad del enriquecido."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_parche_batch_e_idempotencia():
    sg = pytest.importorskip("pipeline.script_generator")
    from pipeline_dist.llm_patch import apply_llm_stability_patches

    ok = apply_llm_stability_patches()
    assert ok is True
    assert sg.ScriptGenerator.ENRICH_BATCH_SIZE == int(
        os.environ.get("AUTOTUBE_ENRICH_BATCH_SIZE", "2")
    )
    wrapped = sg.ScriptGenerator._enrich_block_fields_batch
    # Segunda llamada: idempotente, no re-envuelve.
    assert apply_llm_stability_patches() is True
    assert sg.ScriptGenerator._enrich_block_fields_batch is wrapped
    assert getattr(sg.ScriptGenerator, "_dist_stability_patched") is True
