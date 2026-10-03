#!/usr/bin/env python3
"""Puente TTS en la casa (adaptación SuperServer, opción A).

En modo `local` este módulo no se usa: el orquestador mantiene su ruta actual.
En modo `superserver` deja la petición en el spool de SuperServer, espera el
resultado en `returns/autotube/<jobId>/result/` y **aplica** los artefactos
(mp3 + timestamps + srt + CTA) en `output/audio`.

La casa es la **única** escritora de `autotube.db`: este puente no la toca. El
orquestador (`phase_tts`) recibe el mismo diccionario que en modo local y sigue
actualizando la DB como siempre.

Contrato:
  - Escribe `<IN_DIR>/<rid>/request.json` (payload que el worker lee) y
    `<SPOOL_DIR>/<rid>.json` (petición del pool). El directorio por petición es
    el contrato real con el adaptador (`server/adapters/autotube.js`), que
    empuja ese dir a `push/in` y ejecuta `--request /work/push/in/<name>`.
  - Espera `returns/autotube/<jobId>/result/estado.json` con `rid` coincidente.
  - Copia los artefactos a `out_dir` (AUDIO_DIR) y escribe el ACK.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

MODE_FILE = os.environ.get("SS_MODE_FILE", "/var/lib/taildeck/projects/autotube.mode")
SPOOL_DIR = os.environ.get("SS_SPOOL_DIR", "/var/lib/taildeck/spool")
RETURNS_ROOT = os.environ.get("SS_RETURNS_DIR", "/var/lib/taildeck/returns")
IN_DIR = os.environ.get("SS_AUTOTUBE_IN", "/var/lib/taildeck/autotube_in")
ACK_DIR = os.path.join(SPOOL_DIR, "acks")


def modo() -> str:
    """Modo del proyecto según el flag de la casa (`superserver` | `local`)."""
    try:
        with open(MODE_FILE, encoding="utf-8") as fh:
            return "superserver" if fh.read().strip() == "superserver" else "local"
    except OSError:
        return "local"


def _fingerprint(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def _write_json_atomic(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _buscar_estado(rid: str, timeout: float, poll: float):
    """Escanea returns/autotube/*/result/estado.json buscando el `rid`."""
    root = os.path.join(RETURNS_ROOT, "autotube")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.isdir(root):
            for name in os.listdir(root):
                result_dir = os.path.join(root, name, "result")
                f = os.path.join(result_dir, "estado.json")
                if not os.path.exists(f):
                    continue
                try:
                    with open(f, encoding="utf-8") as fh:
                        est = json.load(fh)
                except (OSError, ValueError):
                    continue
                if est.get("rid") == rid:
                    return name, result_dir, est
        time.sleep(poll)
    return None, None, None


def _escribir_ack(job_id: str, source: str = "tts_bridge") -> None:
    _write_json_atomic(os.path.join(ACK_DIR, f"{job_id}.json"),
                       {"jobId": job_id, "status": "applied", "ts": time.time(), "source": source})


def _copy(src_dir: str, out_dir: str, name: str) -> str:
    dst = os.path.join(out_dir, name)
    shutil.copy2(os.path.join(src_dir, name), dst)
    return dst


def delegar(bloques: list, voice_config: dict, video_id=None, out_dir: str = ".",
            cta_text: str | None = None, timeout: float = 3600.0,
            poll: float = 5.0) -> dict:
    """Delega la síntesis al pool y aplica el resultado. Devuelve el mismo
    diccionario que la ruta local de `phase_tts`."""
    rid = f"tts-{int(time.time())}-{os.getpid()}-{video_id or 'v'}"
    base_name = f"narration_{rid}"
    payload = {
        "bloques": bloques,
        "voice_config": voice_config or {},
        "output_base": base_name,
        "rid": rid,
        "cta_text": cta_text or "",
    }
    # Un DIRECTORIO por petición: el adaptador empuja ese dir a push/in y el
    # worker lee el `request.json` que contiene. Un fichero plano hacía que el
    # adaptador empujara TODO el IN_DIR y el worker buscara un request.json
    # inexistente (bug de contrato, jobs mursepju-…/musahgfx-…/musfqiya-…).
    req_dir = os.path.join(IN_DIR, rid)
    req_file = os.path.join(req_dir, "request.json")
    _write_json_atomic(req_file, payload)

    request = {
        "id": rid,
        "project": "autotube",
        "process": "tts_kokoro",
        "externalId": f"tts:{video_id or rid}",
        "params": {"requestFile": req_file, "outBase": base_name},
        "fingerprint": _fingerprint({"bloques": bloques, "voice_config": voice_config,
                                     "cta_text": cta_text or ""}),
    }
    _write_json_atomic(os.path.join(SPOOL_DIR, f"{rid}.json"), request)

    job_id, result_dir, est = _buscar_estado(rid, timeout, poll)
    if not est or not est.get("ok"):
        raise RuntimeError(f"TTS delegado sin resultado ok (rid={rid}, job={job_id}): {est}")

    os.makedirs(out_dir, exist_ok=True)
    art = est.get("artefactos") or {}
    audio_path = _copy(result_dir, out_dir, art["mp3"])
    ts_path = _copy(result_dir, out_dir, art["timestamps"])
    if art.get("srt"):
        _copy(result_dir, out_dir, art["srt"])
    with open(ts_path, encoding="utf-8") as fh:
        timestamps = json.load(fh)

    cta_audio_path = None
    cta = est.get("cta")
    if cta:
        cta_audio_path = _copy(result_dir, out_dir, cta["mp3"])
        _copy(result_dir, out_dir, cta["timestamps"])

    if job_id:
        _escribir_ack(job_id)
    return {"audio_path": audio_path, "timestamps_path": ts_path,
            "timestamps": timestamps, "cta_audio_path": cta_audio_path, "rid": rid}
