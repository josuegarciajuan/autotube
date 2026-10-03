"""TTS worker sin DB (SuperServer): contrato de artefactos y estado."""
import json
import os

import pytest

from api.services import tts_worker as W


class FakeEngine:
    def __init__(self, voice_config):
        self.kokoro_voice = voice_config.get("kokoro_voice", "em_santa")

    def generate_segmented(self, bloques, output_path=None, progress_cb=None):
        base = os.path.splitext(output_path)[0]
        with open(base + ".mp3", "wb") as fh:
            fh.write(b"MP3")
        with open(base + "_timestamps.json", "w") as fh:
            fh.write("[]")
        with open(base + "_subtitles.srt", "w") as fh:
            fh.write("")
        return base + ".mp3", [{"word": "hola", "start_ms": 0, "end_ms": 1500}]


def _factory(vc):
    return FakeEngine(vc)


def test_run_genera_artefactos_y_estado(tmp_path):
    req = {
        "bloques": [{"tipo": "hook", "texto": "hola"}],
        "voice_config": {"kokoro_voice": "ef_dora"},
        "output_base": "narration_42",
    }
    est = W.run(req, str(tmp_path), engine_factory=_factory)
    assert est["ok"] is True
    assert est["voice"] == "ef_dora"
    assert est["bloques"] == 1
    assert est["duration_s"] == 1.5
    for k in ("mp3", "timestamps", "srt"):
        assert os.path.exists(os.path.join(str(tmp_path), est["artefactos"][k]))
    with open(os.path.join(str(tmp_path), "estado.json")) as fh:
        assert json.load(fh)["ok"] is True


def test_run_sin_bloques_falla(tmp_path):
    with pytest.raises(ValueError, match="bloques"):
        W.run({"bloques": []}, str(tmp_path), engine_factory=_factory)


def test_main_error_escribe_estado(tmp_path):
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"bloques": []}))
    out = tmp_path / "out"
    rc = W.main(["--request", str(req), "--out-dir", str(out)])
    assert rc == 1
    with open(out / "estado.json") as fh:
        assert json.load(fh)["ok"] is False
