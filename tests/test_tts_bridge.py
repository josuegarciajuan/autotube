"""Puente TTS de la casa (SuperServer): spool, espera, verificación y aplicación.

Fase 1: la casa verifica el resultado remoto (tamaño/sha256 de mp3, timestamps
válidos y monótonos, CTA) y clasifica los fallos con `TTSDelegationError.code`.
"""
import json
import os
import time

import pytest

from api.services import tts_bridge as B

# MP3 sintético por encima del mínimo de validación (4096 bytes).
MP3_BYTES = b"ID3" + b"\x00" * 5000


def test_modo_lee_flag(tmp_path, monkeypatch):
    mode = tmp_path / "autotube.mode"
    monkeypatch.setattr(B, "MODE_FILE", str(mode))
    mode.write_text("local")
    assert B.modo() == "local"
    mode.write_text("superserver\n")
    assert B.modo() == "superserver"
    mode.unlink()
    assert B.modo() == "local"  # sin flag => local


def _preparar_resultado(result_dir, rid, base, *, cta=True, files_meta=True, ts=None,
                        mp3_bytes=MP3_BYTES):
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / f"{base}.mp3").write_bytes(mp3_bytes)
    ts = ts if ts is not None else [{"word": "hola", "start_ms": 0, "end_ms": 1000}]
    ts_blob = json.dumps(ts)
    (result_dir / f"{base}_timestamps.json").write_text(ts_blob)
    (result_dir / f"{base}_subtitles.srt").write_text("")
    estado = {
        "ok": True, "rid": rid, "bloques": 1, "duration_s": 1.0, "elapsed_s": 1.0,
        "node": "oficina", "image": "autotube-tts:1",
        "artefactos": {"mp3": f"{base}.mp3", "timestamps": f"{base}_timestamps.json",
                       "srt": f"{base}_subtitles.srt"},
    }
    if cta:
        (result_dir / f"{base}_cta.mp3").write_bytes(MP3_BYTES)
        (result_dir / f"{base}_cta_timestamps.json").write_text('[{"start_ms":0,"end_ms":100}]')
        estado["cta"] = {"mp3": f"{base}_cta.mp3", "timestamps": f"{base}_cta_timestamps.json"}
    if files_meta:
        estado["files"] = {
            "mp3": {"bytes": len(mp3_bytes), "sha256": B._sha256(str(result_dir / f"{base}.mp3"))},
            "timestamps": {"bytes": len(ts_blob.encode()), "entries": len(ts)},
        }
    (result_dir / "estado.json").write_text(json.dumps(estado))
    return estado


def _patch_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "IN_DIR", str(tmp_path / "in"))
    monkeypatch.setattr(B, "SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(B, "ACK_DIR", str(tmp_path / "spool" / "acks"))
    monkeypatch.setattr(B, "_escribir_ack", lambda *a, **k: None)
    # Aísla la certificación (Fase 2) y la salud de nodos (Fase 3).
    monkeypatch.setenv("AUTOTUBE_TTS_CERT_FILE", str(tmp_path / "no_cert.json"))
    monkeypatch.setenv("AUTOTUBE_TTS_HEALTH_FILE", str(tmp_path / "health.json"))
    monkeypatch.delenv("AUTOTUBE_TTS_ALLOWED_NODES", raising=False)
    monkeypatch.delenv("AUTOTUBE_TTS_CERT_TTL_SEC", raising=False)
    monkeypatch.delenv("AUTOTUBE_TTS_QUARANTINE_AFTER", raising=False)


def _write_cert(tmp_path, monkeypatch, nodes, *, ok=True, ttl_hours=24):
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    record = {
        "ts": now, "ttl_hours": ttl_hours, "image": "autotube-tts:1",
        "nodes": {n: {"ok": ok, "ts": now, "error": None} for n in nodes},
    }
    p = tmp_path / "cert.json"
    p.write_text(json.dumps(record))
    monkeypatch.setenv("AUTOTUBE_TTS_CERT_FILE", str(p))
    return p


def _fake_buscar(tmp_path, *, cta=True, files_meta=True, ts=None, mp3_bytes=MP3_BYTES):
    box = {}

    def fake(rid, timeout, poll):
        base = "narration_" + rid
        rdir = tmp_path / "ret" / base
        estado = _preparar_resultado(rdir, rid, base, cta=cta, files_meta=files_meta,
                                     ts=ts, mp3_bytes=mp3_bytes)
        box["base"] = base
        return "job123", str(rdir), estado

    return fake, box


def test_delegar_copia_artefactos(tmp_path, monkeypatch):
    out = tmp_path / "audio"
    _patch_dirs(tmp_path, monkeypatch)
    fake, box = _fake_buscar(tmp_path)
    monkeypatch.setattr(B, "_buscar_estado", fake)

    res = B.delegar([{"tipo": "hook", "texto": "hola"}], {"kokoro_voice": "em_santa"},
                    video_id=42, out_dir=str(out))
    base = box["base"]
    assert os.path.exists(out / f"{base}.mp3")
    assert os.path.exists(out / f"{base}_timestamps.json")
    assert res["audio_path"].endswith(f"{base}.mp3")
    assert res["timestamps"] == [{"word": "hola", "start_ms": 0, "end_ms": 1000}]
    assert res["cta_audio_path"].endswith(f"{base}_cta.mp3")
    # Fase 1: diagnóstico aditivo disponible para el llamante.
    assert res["node"] == "oficina"
    assert res["rid"]
    spool = list((tmp_path / "spool").glob("*.json"))
    assert len(spool) == 1
    req = json.loads(spool[0].read_text())
    assert req["project"] == "autotube" and req["process"] == "tts_kokoro"
    req_file = req["params"]["requestFile"]
    assert os.path.basename(req_file) == "request.json"
    assert os.path.exists(req_file)
    assert os.path.dirname(req_file) != str(tmp_path / "in")
    payload = json.loads(open(req_file, encoding="utf-8").read())
    assert payload["rid"] == req["id"]
    assert payload["output_base"] == base


def test_delegar_incluye_allowed_nodes_de_entorno(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOTUBE_TTS_ALLOWED_NODES", "mail, dedi3133109 ,, ")
    seen = {}

    def fake(rid, timeout, poll):
        seen["req"] = json.loads((tmp_path / "spool" / f"{rid}.json").read_text())
        base = "narration_" + rid
        rdir = tmp_path / "ret" / base
        return "j", str(rdir), _preparar_resultado(rdir, rid, base)

    monkeypatch.setattr(B, "_buscar_estado", fake)
    B.delegar([{"tipo": "hook", "texto": "hola"}], {}, video_id=1, out_dir=str(tmp_path / "audio"))
    assert seen["req"]["allowedNodes"] == ["mail", "dedi3133109"]


def test_delegar_sin_env_no_anade_allowed_nodes(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.delenv("AUTOTUBE_TTS_ALLOWED_NODES", raising=False)
    seen = {}

    def fake(rid, timeout, poll):
        seen["req"] = json.loads((tmp_path / "spool" / f"{rid}.json").read_text())
        base = "narration_" + rid
        rdir = tmp_path / "ret" / base
        return "j", str(rdir), _preparar_resultado(rdir, rid, base)

    monkeypatch.setattr(B, "_buscar_estado", fake)
    B.delegar([{"tipo": "hook", "texto": "hola"}], {}, video_id=1, out_dir=str(tmp_path / "audio"))
    assert "allowedNodes" not in seen["req"]


def test_delegar_sin_resultado_ok_falla(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(B, "_buscar_estado", lambda *a, **k: (None, None, {"ok": False, "error": "x"}))
    with pytest.raises(B.TTSDelegationError) as exc:
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert exc.value.code == "worker_failed"


def test_bridge_timeout_clasifica(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(B, "_buscar_estado", lambda *a, **k: (None, None, None))
    with pytest.raises(B.TTSDelegationError) as exc:
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert exc.value.code == "bridge_timeout"


def test_worker_error_code_se_propaga(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(B, "_buscar_estado", lambda *a, **k: (
        "j", "/tmp", {"ok": False, "error_code": "no_audio", "error": "No blocks produced audio"}
    ))
    with pytest.raises(B.TTSDelegationError) as exc:
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert exc.value.code == "no_audio"


def test_rechaza_mp3_demasiado_pequeno(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    fake, _ = _fake_buscar(tmp_path, mp3_bytes=b"abc")
    monkeypatch.setattr(B, "_buscar_estado", fake)
    with pytest.raises(B.TTSDelegationError) as exc:
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert exc.value.code == "artifact_invalid"


def test_rechaza_timestamps_no_monotonos(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    bad = [{"start_ms": 0, "end_ms": 5000}, {"start_ms": 100, "end_ms": 200}]
    fake, _ = _fake_buscar(tmp_path, ts=bad)
    monkeypatch.setattr(B, "_buscar_estado", fake)
    with pytest.raises(B.TTSDelegationError) as exc:
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert exc.value.code == "artifact_invalid"


def test_rechaza_sha256_que_no_coincide(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    box = {}

    def fake(rid, timeout, poll):
        base = "narration_" + rid
        rdir = tmp_path / "ret" / base
        est = _preparar_resultado(rdir, rid, base)
        est["files"]["mp3"]["sha256"] = "0" * 64
        (rdir / "estado.json").write_text(json.dumps(est))
        box["base"] = base
        return "j", str(rdir), est

    monkeypatch.setattr(B, "_buscar_estado", fake)
    with pytest.raises(B.TTSDelegationError) as exc:
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert exc.value.code == "artifact_mismatch"


def test_cta_faltante_es_incidencia_no_fallo(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    fake, box = _fake_buscar(tmp_path, cta=False)
    monkeypatch.setattr(B, "_buscar_estado", fake)
    res = B.delegar([{"texto": "x"}], {}, cta_text="suscríbete",
                    out_dir=str(tmp_path / "audio"))
    assert res["cta_audio_path"] is None
    assert res["cta_incident"] == "cta_missing"


def test_resultado_antiguo_sin_files_se_acepta(tmp_path, monkeypatch):
    """Compatibilidad: un worker antiguo (sin `files`) sigue aplicándose."""
    _patch_dirs(tmp_path, monkeypatch)
    fake, box = _fake_buscar(tmp_path, files_meta=False)
    monkeypatch.setattr(B, "_buscar_estado", fake)
    res = B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert os.path.exists(res["audio_path"])


# ── Fase 2: certificación funcional ────────────────────────────────

def test_allowed_nodes_se_intersecta_con_certificacion(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOTUBE_TTS_ALLOWED_NODES", "mail,oficina,dedi3133109")
    _write_cert(tmp_path, monkeypatch, ["oficina", "mail"])
    seen = {}

    def fake(rid, timeout, poll):
        seen["req"] = json.loads((tmp_path / "spool" / f"{rid}.json").read_text())
        base = "narration_" + rid
        rdir = tmp_path / "ret" / base
        return "j", str(rdir), _preparar_resultado(rdir, rid, base)

    monkeypatch.setattr(B, "_buscar_estado", fake)
    B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    # dedi3133109 no está certificado → excluido.
    assert set(seen["req"]["allowedNodes"]) == {"mail", "oficina"}


def test_sin_nodo_certificado_no_delega(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOTUBE_TTS_ALLOWED_NODES", "dedi3133109")
    _write_cert(tmp_path, monkeypatch, ["mail", "oficina"])
    with pytest.raises(B.TTSDelegationError) as exc:
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert exc.value.code == "no_certified_node"
    # No se llegó a escribir ninguna petición en el spool.
    assert not list((tmp_path / "spool").glob("*.json"))


def test_certificacion_expirada_no_certifica(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOTUBE_TTS_ALLOWED_NODES", "mail")
    monkeypatch.setenv("AUTOTUBE_TTS_CERT_TTL_SEC", "1")
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3600))
    p = tmp_path / "cert.json"
    p.write_text(json.dumps({"ts": old, "ttl_hours": 24,
                             "nodes": {"mail": {"ok": True, "ts": old}}}))
    monkeypatch.setenv("AUTOTUBE_TTS_CERT_FILE", str(p))
    with pytest.raises(B.TTSDelegationError) as exc:
        B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert exc.value.code == "no_certified_node"


# ── Fase 3: salud y cuarentena ─────────────────────────────────────

def test_nodo_fallido_se_excluye_del_siguiente_intento(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOTUBE_TTS_ALLOWED_NODES", "mail,oficina")
    _write_cert(tmp_path, monkeypatch, ["mail", "oficina"])
    monkeypatch.setattr(B, "_default_retries", lambda: 1)
    calls = {"n": 0}
    seen = []

    def fake(rid, timeout, poll):
        calls["n"] += 1
        req = json.loads((tmp_path / "spool" / f"{rid}.json").read_text())
        seen.append(req.get("allowedNodes"))
        base = "narration_" + rid
        rdir = tmp_path / "ret" / base
        if calls["n"] == 1:
            return "j1", str(rdir), {"ok": False, "node": "mail",
                                     "error_code": "container_start_failed", "error": "exit 125"}
        return "j2", str(rdir), _preparar_resultado(rdir, rid, base)

    monkeypatch.setattr(B, "_buscar_estado", fake)
    res = B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert res["audio_path"]
    assert seen[0] == ["mail", "oficina"]         # 1er intento: ambos
    assert seen[1] == ["oficina"]                 # 2º intento: mail excluido
    from api.services import tts_node_health as H
    assert H.state_of("mail") == H.DEGRADED


def test_cuarentena_tras_dos_fallos(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    from api.services import tts_node_health as H
    assert H.record_failure("mail", "container_start_failed") == H.DEGRADED
    assert H.record_failure("mail", "container_start_failed") == H.QUARANTINED
    assert H.is_available("mail") is False
    # Un nodo en cuarentena no entra en la elegibilidad aunque esté certificado.
    monkeypatch.setenv("AUTOTUBE_TTS_ALLOWED_NODES", "mail,oficina")
    _write_cert(tmp_path, monkeypatch, ["mail", "oficina"])
    eligible, _ = B._eligible_tts_nodes()
    assert eligible == ["oficina"]


def test_certificacion_reincorpora_nodo_en_cuarentena(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    from api.services import tts_node_health as H
    H.record_failure("mail", "x")
    H.record_failure("mail", "x")
    assert H.state_of("mail") == H.QUARANTINED
    monkeypatch.setenv("AUTOTUBE_TTS_ALLOWED_NODES", "mail")
    _write_cert(tmp_path, monkeypatch, ["mail"])
    # La reincorporación ocurre al inicio del job, no en la consulta de elegibles.
    H.reconcile_with_certification({"mail"})
    eligible, _ = B._eligible_tts_nodes()
    assert eligible == ["mail"]
    assert H.state_of("mail") == H.HEALTHY


def test_sin_registro_certificacion_es_legacy(tmp_path, monkeypatch):
    _patch_dirs(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOTUBE_TTS_ALLOWED_NODES", "mail,oficina")
    fake, _ = _fake_buscar(tmp_path)
    seen = {}

    def wrap(rid, timeout, poll):
        seen["req"] = json.loads((tmp_path / "spool" / f"{rid}.json").read_text())
        return fake(rid, timeout, poll)

    monkeypatch.setattr(B, "_buscar_estado", wrap)
    B.delegar([{"texto": "x"}], {}, out_dir=str(tmp_path / "audio"))
    assert seen["req"]["allowedNodes"] == ["mail", "oficina"]
