"""Fase 5: fallo rápido del proveedor edge-tts (401/403) → Kokoro local."""
import pytest

from pipeline import tts_engine as TE


@pytest.mark.parametrize("msg", [
    "403, message='Invalid response status'",
    "401 Unauthorized",
    "Forbidden",
    "Sign in to confirm you're not a bot",
    "Invalid response status",
])
def test_detecta_rechazo_de_proveedor(msg):
    assert TE._looks_like_provider_rejection(msg) is True


@pytest.mark.parametrize("msg", ["connection reset", "timed out", "pydub error"])
def test_no_confunde_fallos_transitorios(msg):
    assert TE._looks_like_provider_rejection(msg) is False


def _engine_with_stream(monkeypatch, exc):
    eng = TE.TTSEngine({})

    def _boom(*a, **k):
        raise exc

    monkeypatch.setattr(eng, "_stream_sync", _boom)
    return eng


def test_aborta_sin_reintentar_por_segmento(monkeypatch):
    eng = _engine_with_stream(monkeypatch, RuntimeError("403, message='Invalid response status'"))
    calls = {"n": 0}
    orig = eng._stream_sync

    def _counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(eng, "_stream_sync", _counting)
    with pytest.raises(TE.TTSEngineAbort):
        eng._synthesize_with_retry("hola", None, None, None, timeout=5)
    # Un único intento: no hay reintento "con defaults" ante rechazo del proveedor.
    assert calls["n"] == 1


def test_transitorio_si_reintenta(monkeypatch):
    """Un fallo no-provider reintenta una vez y, si vuelve a fallar, devuelve vacío."""
    eng = TE.TTSEngine({})
    state = {"n": 0}

    def _boom(*a, **k):
        state["n"] += 1
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(eng, "_stream_sync", _boom)
    audio, ts = eng._synthesize_with_retry("hola", None, None, None, timeout=5)
    assert (audio, ts) == ([], [])
    assert state["n"] == 2
