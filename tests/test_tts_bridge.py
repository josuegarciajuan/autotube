"""Puente TTS de la casa (SuperServer): spool, espera y aplicación de artefactos."""
import json
import os

import pytest

from api.services import tts_bridge as B


def test_modo_lee_flag(tmp_path, monkeypatch):
    mode = tmp_path / "autotube.mode"
    monkeypatch.setattr(B, "MODE_FILE", str(mode))
    mode.write_text("local")
    assert B.modo() == "local"
    mode.write_text("superserver\n")
    assert B.modo() == "superserver"
    mode.unlink()
    assert B.modo() == "local"  # sin flag => local


def _preparar_resultado(result_dir, rid, base):
    result_dir.mkdir(parents=True)
    (result_dir / f"{base}.mp3").write_bytes(b"MP3")
    (result_dir / f"{base}_timestamps.json").write_text('[{"word":"hola","start_ms":0,"end_ms":1000}]')
    (result_dir / f"{base}_subtitles.srt").write_text("")
    (result_dir / f"{base}_cta.mp3").write_bytes(b"CTA")
    (result_dir / f"{base}_cta_timestamps.json").write_text("[]")
    estado = {
        "ok": True, "rid": rid, "bloques": 1, "duration_s": 1.0,
        "artefactos": {"mp3": f"{base}.mp3", "timestamps": f"{base}_timestamps.json",
                       "srt": f"{base}_subtitles.srt"},
        "cta": {"mp3": f"{base}_cta.mp3", "timestamps": f"{base}_cta_timestamps.json"},
    }
    (result_dir / "estado.json").write_text(json.dumps(estado))
    return estado


def test_delegar_copia_artefactos(tmp_path, monkeypatch):
    out = tmp_path / "audio"
    monkeypatch.setattr(B, "IN_DIR", str(tmp_path / "in"))
    monkeypatch.setattr(B, "SPOOL_DIR", str(tmp_path / "spool"))
    # simula el retorno ya presente ignorando el rid generado
    rid_box = {}

    def fake_buscar(rid, timeout, poll):
        base = "narration_" + rid
        rdir = tmp_path / "ret" / base
        estado = _preparar_resultado(rdir, rid, base)
        rid_box["base"] = base
        return "job123", str(rdir), estado

    acked = {}
    monkeypatch.setattr(B, "_buscar_estado", fake_buscar)
    monkeypatch.setattr(B, "_escribir_ack", lambda jid, source="tts_bridge": acked.update(job=jid))

    res = B.delegar([{"tipo": "hook", "texto": "hola"}], {"kokoro_voice": "em_santa"},
                    video_id=42, out_dir=str(out))
    base = rid_box["base"]
    assert os.path.exists(out / f"{base}.mp3")
    assert os.path.exists(out / f"{base}_timestamps.json")
    assert res["audio_path"].endswith(f"{base}.mp3")
    assert res["timestamps"] == [{"word": "hola", "start_ms": 0, "end_ms": 1000}]
    assert res["cta_audio_path"].endswith(f"{base}_cta.mp3")
    assert acked["job"] == "job123"
    # la petición quedó en el spool para que el plano la recoja
    spool = list((tmp_path / "spool").glob("*.json"))
    assert len(spool) == 1
    req = json.loads(spool[0].read_text())
    assert req["project"] == "autotube" and req["process"] == "tts_kokoro"
    # Contrato con el adaptador: un dir por rid con `request.json` dentro.
    req_file = req["params"]["requestFile"]
    assert os.path.basename(req_file) == "request.json"
    assert os.path.exists(req_file)
    assert os.path.dirname(req_file) != str(tmp_path / "in")
    payload = json.loads(open(req_file, encoding="utf-8").read())
    assert payload["rid"] == req["id"]
    assert payload["output_base"] == base


def test_delegar_sin_resultado_ok_falla(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "IN_DIR", str(tmp_path / "in"))
    monkeypatch.setattr(B, "SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(B, "_buscar_estado", lambda *a, **k: (None, None, {"ok": False, "error": "x"}))
    with pytest.raises(RuntimeError, match="sin resultado ok"):
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
