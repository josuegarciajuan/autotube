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

Fase 1 (observabilidad y contrato de resultado):
  - El estado remoto se **verifica** antes de aplicarse: existencia y tamaño de
    mp3, JSON de timestamps válido y monótono, y (si el worker lo reporta)
    `bytes`/`sha256` coincidentes.
  - Todo fallo se **clasifica** con un `error_code` estructurado y se adjunta el
    nodo ejecutor, para que el fallback local no dependa de adivinar la causa.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time

logger = logging.getLogger("autotube.tts_bridge")

MODE_FILE = os.environ.get("SS_MODE_FILE", "/var/lib/taildeck/projects/autotube.mode")
SPOOL_DIR = os.environ.get("SS_SPOOL_DIR", "/var/lib/taildeck/spool")
RETURNS_ROOT = os.environ.get("SS_RETURNS_DIR", "/var/lib/taildeck/returns")
IN_DIR = os.environ.get("SS_AUTOTUBE_IN", "/var/lib/taildeck/autotube_in")
ACK_DIR = os.path.join(SPOOL_DIR, "acks")

# Mínimos de validación (los mismos que el worker remoto). Un artefacto por
# debajo del umbral se considera inválido: nunca se aplica a medias.
MIN_MP3_BYTES = 4096


class TTSDelegationError(RuntimeError):
    """Fallo clasificado de la delegación TTS (lleva `code` + diagnóstico)."""

    def __init__(self, code: str, message: str, diagnostic: dict | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic or {}


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


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


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


def _default_timeout() -> float:
    try:
        return max(300.0, float(os.getenv("TTS_POOL_TIMEOUT_SEC", "3000")))
    except (TypeError, ValueError):
        return 3000.0


def _default_retries() -> int:
    try:
        return max(0, int(os.getenv("TTS_POOL_RETRIES", "1")))
    except (TypeError, ValueError):
        return 1


def _tts_allowed_nodes() -> list:
    """Allowlist de nodos para el TTS (denylist de facto de la casa).

    Lista separada por comas en ``AUTOTUBE_TTS_ALLOWED_NODES``. Sirve para que el
    TTS delegado NO caiga en la casa (`liveyourdre2`) aunque tenga el tag
    `kokoro`: el scheduler del pool solo entiende allowlist. Vacío/ausente =
    sin restricción (comportamiento legacy). Nombres inexistentes los rechaza el
    spool (fail-closed).
    """
    raw = os.environ.get("AUTOTUBE_TTS_ALLOWED_NODES", "")
    return [n.strip() for n in str(raw).split(",") if n.strip()]


# ── Verificación de artefactos (Fase 1) ────────────────────────────

def _expected_files(est: dict) -> dict:
    files = est.get("files")
    return files if isinstance(files, dict) else {}


def _check_file(result_dir: str, name: str, role: str, est: dict, *, min_bytes: int) -> str:
    """Verifica existencia/tamaño/sha256 de un artefacto. Devuelve su ruta."""
    path = os.path.join(result_dir, name)
    if not os.path.exists(path):
        raise TTSDelegationError(
            "artifact_missing", f"{role} no existe en el retorno: {name}",
            {"role": role, "name": name},
        )
    size = os.path.getsize(path)
    if size < min_bytes:
        raise TTSDelegationError(
            "artifact_invalid", f"{role} demasiado pequeño ({size} < {min_bytes})",
            {"role": role, "name": name, "bytes": size},
        )
    meta = _expected_files(est).get(role)
    if isinstance(meta, dict):
        exp_bytes = meta.get("bytes")
        if isinstance(exp_bytes, int) and exp_bytes != size:
            raise TTSDelegationError(
                "artifact_mismatch",
                f"{role} bytes no coinciden ({size} != {exp_bytes})",
                {"role": role, "name": name, "bytes": size, "expected_bytes": exp_bytes},
            )
        exp_sha = str(meta.get("sha256") or "")
        if exp_sha:
            actual = _sha256(path)
            if actual != exp_sha:
                raise TTSDelegationError(
                    "artifact_mismatch",
                    f"{role} sha256 no coincide ({actual[:12]} != {exp_sha[:12]})",
                    {"role": role, "name": name, "sha256": actual, "expected_sha256": exp_sha},
                )
    return path


def _load_timestamps(ts_path: str) -> list:
    try:
        with open(ts_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise TTSDelegationError("artifact_invalid", f"timestamps ilegible: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise TTSDelegationError("artifact_invalid", "timestamps vacío o no es lista")
    last_end = -1.0
    for i, ts in enumerate(data):
        if not isinstance(ts, dict):
            raise TTSDelegationError("artifact_invalid", f"timestamp[{i}] no es objeto")
        try:
            start = float(ts.get("start_ms"))
            end = float(ts.get("end_ms"))
        except (TypeError, ValueError):
            raise TTSDelegationError("artifact_invalid", f"timestamp[{i}] no numérico")
        if start < 0 or end < start:
            raise TTSDelegationError(
                "artifact_invalid", f"timestamp[{i}] inválido (start={start}, end={end})"
            )
        if end + 1.0 < last_end:
            raise TTSDelegationError(
                "artifact_invalid", f"timestamps no monótonos en [{i}] (end={end} < prev={last_end})"
            )
        last_end = max(last_end, end)
    if last_end <= 0:
        raise TTSDelegationError("artifact_invalid", "timestamps sin duración")
    return data


def _validate_result(est: dict, result_dir: str, cta_requested: bool) -> tuple[list, str | None]:
    """Verifica el estado ok del worker. Devuelve (timestamps, incidencia_cta).

    Lanza `TTSDelegationError` si el resultado no es aplicable. La incidencia de
    CTA es NON-FATAL (paridad con la ruta local): se devuelve como texto.
    """
    art = est.get("artefactos")
    if not isinstance(art, dict):
        raise TTSDelegationError("artifact_invalid", "estado sin bloque 'artefactos'")
    mp3 = art.get("mp3")
    ts = art.get("timestamps")
    if not mp3 or not ts:
        raise TTSDelegationError("artifact_invalid", "faltan 'mp3'/'timestamps' en artefactos")
    _check_file(result_dir, mp3, "mp3", est, min_bytes=MIN_MP3_BYTES)
    ts_path = _check_file(result_dir, ts, "timestamps", est, min_bytes=1)
    timestamps = _load_timestamps(ts_path)

    cta = est.get("cta")
    cta_incident = None
    if cta_requested:
        if not isinstance(cta, dict) or not cta.get("mp3"):
            cta_incident = "cta_missing"
        else:
            _check_file(result_dir, cta["mp3"], "cta_mp3", est, min_bytes=MIN_MP3_BYTES)
            if cta.get("timestamps"):
                _check_file(result_dir, cta["timestamps"], "cta_timestamps", est, min_bytes=1)
    return timestamps, cta_incident


def delegar(bloques: list, voice_config: dict, video_id=None, out_dir: str = ".",
            cta_text: str | None = None, timeout: float | None = None,
            poll: float = 5.0, retries: int | None = None) -> dict:
    """Delega la síntesis al pool y aplica el resultado verificado. Devuelve el
    mismo diccionario que la ruta local de `phase_tts` (más claves de diagnóstico).

    El timeout (`TTS_POOL_TIMEOUT_SEC`, default 3000s) cubre jobs reales del pool
    (1600-2350s). Ante fallo de transporte se reintenta (`TTS_POOL_RETRIES`); el
    fallback local de `orchestrator._tts_delegado` cubre el resto.

    Fase 1: cada intento verifica el resultado antes de aplicarlo y clasifica el
    error con `TTSDelegationError.code` (`bridge_timeout`, `worker_failed`,
    `artifact_invalid`, `artifact_missing`, `artifact_mismatch`, …).
    """
    if timeout is None:
        timeout = _default_timeout()
    if retries is None:
        retries = _default_retries()
    job_id = result_dir = est = None
    last_code = "bridge_timeout"
    last_diag: dict = {}
    last_error = "sin resultado"
    rid = ""
    for attempt in range(retries + 1):
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
        # worker lee el `request.json` que contiene.
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
        allowed_nodes = _tts_allowed_nodes()
        if allowed_nodes:
            request["allowedNodes"] = allowed_nodes
        _write_json_atomic(os.path.join(SPOOL_DIR, f"{rid}.json"), request)

        job_id, result_dir, est = _buscar_estado(rid, timeout, poll)
        if est and est.get("ok"):
            try:
                timestamps, cta_incident = _validate_result(est, result_dir, bool(cta_text))
                return _apply_result(
                    est, result_dir, out_dir, job_id, rid, timestamps, cta_incident
                )
            except TTSDelegationError as exc:
                last_code, last_diag, last_error = exc.code, exc.diagnostic, str(exc)
                logger.error(
                    "TTS pool resultado INVÁLIDO (rid=%s, job=%s, node=%s): %s",
                    rid, job_id, (est or {}).get("node"), exc,
                )
        elif est:
            last_code = str(est.get("error_code") or "worker_failed")
            last_diag = {
                "node": est.get("node"), "image": est.get("image"),
                "error": est.get("error"),
            }
            last_error = f"worker falló ({last_code}): {est.get('error')}"
            logger.warning(
                "TTS pool sin resultado ok (intento %d/%d, rid=%s, job=%s, node=%s, code=%s): %s",
                attempt + 1, retries + 1, rid, job_id, est.get("node"), last_code, est.get("error"),
            )
        else:
            last_code, last_diag = "bridge_timeout", {"timeout_s": timeout}
            last_error = f"sin estado.json tras {timeout}s (job={job_id})"
            logger.warning(
                "TTS pool sin resultado ok (intento %d/%d, rid=%s, job=%s): timeout %ss",
                attempt + 1, retries + 1, rid, job_id, timeout,
            )
    raise TTSDelegationError(
        last_code,
        f"TTS delegado falló (rid={rid}, job={job_id}): {last_error}",
        {"rid": rid, "job_id": job_id, **last_diag},
    )


def _apply_result(est: dict, result_dir: str, out_dir: str, job_id, rid: str,
                  timestamps: list, cta_incident: str | None) -> dict:
    """Copia los artefactos verificados a `out_dir` y devuelve el dict de fase."""
    os.makedirs(out_dir, exist_ok=True)
    art = est.get("artefactos") or {}
    audio_path = _copy(result_dir, out_dir, art["mp3"])
    ts_path = _copy(result_dir, out_dir, art["timestamps"])
    if art.get("srt"):
        _copy(result_dir, out_dir, art["srt"])

    cta_audio_path = None
    cta = est.get("cta")
    if isinstance(cta, dict) and cta.get("mp3"):
        cta_audio_path = _copy(result_dir, out_dir, cta["mp3"])
        if cta.get("timestamps"):
            _copy(result_dir, out_dir, cta["timestamps"])

    if job_id:
        _escribir_ack(job_id)
    return {
        "audio_path": audio_path,
        "timestamps_path": ts_path,
        "timestamps": timestamps,
        "cta_audio_path": cta_audio_path,
        "rid": rid,
        # ── Fase 1: diagnóstico (aditivo; `phase_tts` lo ignora) ──
        "node": est.get("node"),
        "image": est.get("image"),
        "elapsed_s": est.get("elapsed_s"),
        "duration_s": est.get("duration_s"),
        "cta_incident": cta_incident,
    }
