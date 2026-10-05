"""Fallback local de TTS cuando la delegación al pool falla.

Si ``tts_bridge.delegar`` lanza, el puente no aplicó artefactos (solo lo hace
con ``estado.json`` ok y ``rid`` coincidente), así que ``_tts_delegado`` puede
devolver ``None`` para que ``phase_tts`` siga por la ruta local Kokoro sin
duplicar audio. Solo con el flag desactivado se re-lanza (comportamiento previo).
"""
from __future__ import annotations

import pytest

from api.services import tts_bridge
from orchestrator import PipelineOrchestrator


class KokoroTTSEngine:
    """Nombre exacto que `_tts_delegado` exige para delegar."""

    kokoro_voice = "em_santa"


class _FakeDB:
    def __init__(self):
        self.logs: list[tuple] = []

    def log_pipeline(self, canal, phase, status, message, **kwargs):
        self.logs.append((canal, phase, status, message))


class _FakeSelf:
    canal = "canalX"

    def __init__(self):
        self.tts = KokoroTTSEngine()
        self.db = _FakeDB()
        self.progress: list[str] = []

    def _emit_progress(self, pct, phase, msg):
        self.progress.append(msg)


def _boom(*args, **kwargs):
    raise RuntimeError("pool caído")


def _prep(monkeypatch):
    monkeypatch.setattr(tts_bridge, "modo", lambda: "superserver")
    monkeypatch.setattr(tts_bridge, "delegar", _boom)


def test_tts_fallback_local_devuelve_none(monkeypatch):
    _prep(monkeypatch)
    monkeypatch.delenv("AUTOTUBE_TTS_LOCAL_FALLBACK", raising=False)
    fake = _FakeSelf()

    out = PipelineOrchestrator._tts_delegado(
        fake, {"id": 1, "video_id": 5}, [{"texto": "hola"}], 0.0,
    )

    assert out is None
    assert fake.db.logs == [], "el camino de fallback no debe tocar la DB"
    assert any("generando en local" in m.lower() for m in fake.progress)


def test_tts_fallback_desactivado_relanza(monkeypatch):
    _prep(monkeypatch)
    monkeypatch.setenv("AUTOTUBE_TTS_LOCAL_FALLBACK", "0")
    fake = _FakeSelf()

    with pytest.raises(RuntimeError):
        PipelineOrchestrator._tts_delegado(
            fake, {"id": 1, "video_id": 5}, [{"texto": "hola"}], 0.0,
        )
    assert len(fake.db.logs) == 1, "con el flag off se mantiene el log de error"


def test_tts_fallback_flag_off_variantes(monkeypatch):
    _prep(monkeypatch)
    for raw in ("false", "no", "off", "FALSE"):
        monkeypatch.setenv("AUTOTUBE_TTS_LOCAL_FALLBACK", raw)
        with pytest.raises(RuntimeError):
            PipelineOrchestrator._tts_delegado(
                _FakeSelf(), {"id": 1}, [{"texto": "x"}], 0.0,
            )
