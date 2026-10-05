"""Regresión: el diferimiento por nicho se marca como NO-fallo (oct 2026).

El guard de nicho estricto difiere un guion cuando no hay fuentes on-niche. Antes
el worker lo convertía en vídeo 'error' + alerta crítica 'failed'. Ahora el
orquestador marca `_script_deferred` y el worker deja el vídeo en 'draft'.
"""
from types import SimpleNamespace


def test_defer_if_off_niche_sets_flag(monkeypatch):
    import orchestrator

    monkeypatch.setattr("pipeline.niche_guard.is_on_niche", lambda t, c: False)
    stub = SimpleNamespace(
        db=object(),
        canal="canal5",
        config=SimpleNamespace(NICHE_GUARD_STRICT=True),
        _emit_niche_defer_alert=lambda reason: None,
        _script_deferred=False,
    )
    assert orchestrator.PipelineOrchestrator._defer_if_off_niche(
        stub, {"titulo": "un tema fuera de nicho"}) is True
    assert stub._script_deferred is True


def test_defer_if_off_niche_on_niche_no_flag(monkeypatch):
    import orchestrator

    monkeypatch.setattr("pipeline.niche_guard.is_on_niche", lambda t, c: True)
    stub = SimpleNamespace(
        db=object(),
        canal="canal5",
        config=SimpleNamespace(NICHE_GUARD_STRICT=True),
        _emit_niche_defer_alert=lambda reason: None,
        _script_deferred=False,
    )
    assert orchestrator.PipelineOrchestrator._defer_if_off_niche(
        stub, {"titulo": "caso médico inexplicable"}) is False
    assert stub._script_deferred is False
