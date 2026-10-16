"""TTS worker remoto (SuperServer): contrato de `estado.json` y validación.

Fase 1: el worker autoidentifica nodo/imagen, describe cada artefacto
(bytes/sha256) y clasifica los fallos con `error_code`.
"""
import json
import os

import pytest

from api.services import tts_worker as W

MP3_BYTES = b"ID3" + b"\x00" * 5000


class _FakeEngine:
    kokoro_voice = "em_santa"

    def __init__(self, *, fail=None, cta_ok=True):
        self._fail = fail
        self._cta_ok = cta_ok

    def generate_segmented(self, bloques, output_path=None):
        if self._fail:
            raise RuntimeError(self._fail)
        with open(f"{output_path}.mp3", "wb") as fh:
            fh.write(MP3_BYTES)
        ts = [{"word": "hola", "start_ms": 0, "end_ms": 1200}]
        with open(f"{output_path}_timestamps.json", "w", encoding="utf-8") as fh:
            json.dump(ts, fh)
        with open(f"{output_path}_subtitles.srt", "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,000 --> 00:00:01,200\nhola\n")
        return f"{output_path}.mp3", ts

    def generate(self, text, output_path=None):
        if not self._cta_ok:
            raise RuntimeError("cta boom")
        with open(f"{output_path}.mp3", "wb") as fh:
            fh.write(MP3_BYTES)
        with open(f"{output_path}_timestamps.json", "w", encoding="utf-8") as fh:
            json.dump([{"start_ms": 0, "end_ms": 300}], fh)
        return f"{output_path}.mp3", []


def _factory(**kw):
    return lambda voice_config: _FakeEngine(**kw)


def test_run_estado_completo(tmp_path, monkeypatch):
    monkeypatch.setenv("TAILDECK_NODE", "oficina")
    monkeypatch.setenv("TAILDECK_AUTOTUBE_IMAGE", "autotube-tts:1")
    monkeypatch.setenv("TAILDECK_AUTOTUBE_IMAGE_ID", "sha256:deadbeef")
    req = {
        "bloques": [{"texto": "hola mundo"}, {"texto": "adios"}],
        "output_base": "narration_test",
        "rid": "tts-123",
        "cta_text": "suscríbete",
    }
    estado = W.run(req, str(tmp_path), engine_factory=_factory())
    assert estado["ok"] is True
    assert estado["node"] == "oficina"
    assert estado["image"] == "autotube-tts:1"
    assert estado["image_id"] == "sha256:deadbeef"
    assert estado["words"] == 3
    assert estado["duration_s"] == 1.2
    assert estado["rid"] == "tts-123"
    # Descripción verificable de artefactos.
    assert estado["files"]["mp3"]["bytes"] == len(MP3_BYTES)
    assert len(estado["files"]["mp3"]["sha256"]) == 64
    assert estado["files"]["timestamps"]["entries"] == 1
    # Contrato original intacto.
    assert estado["artefactos"]["mp3"] == "narration_test.mp3"
    assert estado["cta"]["mp3"] == "narration_test_cta.mp3"
    # estado.json en disco.
    on_disk = json.loads((tmp_path / "estado.json").read_text())
    assert on_disk["ok"] is True and on_disk["node"] == "oficina"


def test_run_bloques_vacios(tmp_path):
    with pytest.raises(W.TTSWorkerError) as exc:
        W.run({"bloques": []}, str(tmp_path), engine_factory=_factory())
    assert exc.value.code == W.ERR_REQUEST_INVALID


def test_run_no_audio_se_clasifica(tmp_path):
    with pytest.raises(W.TTSWorkerError) as exc:
        W.run({"bloques": [{"texto": "x"}]}, str(tmp_path),
              engine_factory=_factory(fail="No blocks produced audio"))
    assert exc.value.code == W.ERR_NO_AUDIO


def test_run_fallo_engine_generico(tmp_path):
    with pytest.raises(W.TTSWorkerError) as exc:
        W.run({"bloques": [{"texto": "x"}]}, str(tmp_path),
              engine_factory=_factory(fail="segfault en torch"))
    assert exc.value.code == W.ERR_KOKORO_RUNTIME


def test_run_artefacto_invalido(tmp_path):
    """Un motor que no deja mp3 válido debe clasificarse como artifact_invalid."""
    class _NoFileEngine:
        kokoro_voice = "v"

        def generate_segmented(self, bloques, output_path=None):
            with open(f"{output_path}_timestamps.json", "w", encoding="utf-8") as fh:
                json.dump([{"start_ms": 0, "end_ms": 100}], fh)
            return f"{output_path}.mp3", [{"start_ms": 0, "end_ms": 100}]

    with pytest.raises(W.TTSWorkerError) as exc:
        W.run({"bloques": [{"texto": "x"}]}, str(tmp_path),
              engine_factory=lambda vc: _NoFileEngine())
    assert exc.value.code == W.ERR_ARTIFACT_INVALID


def test_cta_fallido_no_es_fatal(tmp_path):
    estado = W.run({"bloques": [{"texto": "x"}], "cta_text": "hola"},
                   str(tmp_path), engine_factory=_factory(cta_ok=False))
    assert estado["ok"] is True
    assert estado["cta"] is None
    assert "cta boom" in (estado["cta_error"] or "")


def test_write_error_estado(tmp_path, monkeypatch):
    monkeypatch.setenv("TAILDECK_NODE", "mail")
    code = W._write_error_estado(
        str(tmp_path), W.TTSWorkerError(W.ERR_ARTIFACT_INVALID, "boom"), 0.0)
    assert code == W.ERR_ARTIFACT_INVALID
    payload = json.loads((tmp_path / "estado.json").read_text())
    assert payload["ok"] is False
    assert payload["error_code"] == W.ERR_ARTIFACT_INVALID
    assert payload["node"] == "mail"
