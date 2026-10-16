#!/usr/bin/env python3
"""TTS Kokoro worker SIN base de datos (adaptación SuperServer, opción A).

Unidad de pool pura: recibe un request JSON, sintetiza con Kokoro y deja los
artefactos en `--out-dir`. NO toca `autotube.db`; la casa es la única que aplica
el resultado (ruta de audio + timestamps) a la DB.

Contrato de entrada (`--request`):
    {
      "bloques": [ {"tipo": "...", "texto": "...", ...}, ... ],   # obligatorio
      "voice_config": { "kokoro_voice": "em_santa", ... },        # opcional
      "output_base": "narration_<videoId>",                       # opcional
      "rid": "tts-...",                                           # opcional
      "cta_text": "..."                                           # opcional
    }

Salida en `--out-dir`:
    <base>.mp3, <base>_timestamps.json, <base>_subtitles.srt, [<base>_cta.mp3 …],
    estado.json

`estado.json` (lo que la casa usa para aplicar y VERIFICAR):
    {
      ok, engine, voice, bloques, words, duration_s, elapsed_s, ts, rid,
      # ── contrato original (basenames) — NO romper: la casa indexa por aquí ──
      artefactos: {mp3, timestamps, srt},
      cta: {mp3, timestamps} | null,
      # ── Fase 1: observabilidad y verificación (nuevo, aditivo) ──
      node, image, image_id, started_at, finished_at,
      files: {
        mp3:        {bytes, sha256},
        timestamps: {bytes, sha256, entries, last_end_ms},
        srt:        {bytes, sha256},
        cta_mp3:    {bytes, sha256},          # si hay CTA
        cta_timestamps: {bytes, sha256, entries},
      },
      cta_error: "..."        # solo si se pidió CTA y falló (no fatal)
    }

En fallo, `estado.json` es `{ok: false, error, error_code, node, image,
started_at, finished_at, ts}`. `error_code` clasifica la causa para que la casa
decida el fallback sin adivinar.

Uso:
    python3 api/services/tts_worker.py --request in/request.json --out-dir out
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable

DEFAULT_BASE = "narration"

# ── Códigos de error estructurados (contrato con la casa) ──────────
ERR_REQUEST_INVALID = "request_invalid"
ERR_KOKORO_RUNTIME = "kokoro_runtime_failed"
ERR_NO_AUDIO = "no_audio"
ERR_ARTIFACT_INVALID = "artifact_invalid"
ERR_UNKNOWN = "unknown"

# Mínimos de validación (fail-safe: un artefacto inválido nunca se aplica).
MIN_MP3_BYTES = 4096


class TTSWorkerError(RuntimeError):
    """Error clasificado del worker: lleva `code` para el `estado.json`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _describe(path: str, *, extra: dict | None = None) -> dict:
    """Describe un artefacto para verificación en la casa (bytes + sha256)."""
    info = {"bytes": os.path.getsize(path), "sha256": _sha256(path)}
    if extra:
        info.update(extra)
    return info


def _engine_factory(voice_config: dict):
    """Import diferido para no cargar torch/kokoro si solo se testea la lógica."""
    from pipeline.kokoro_tts import KokoroTTSEngine

    return KokoroTTSEngine(voice_config)


def _count_words(bloques: list) -> int:
    total = 0
    for b in bloques or []:
        if isinstance(b, dict):
            total += len(str(b.get("texto") or "").split())
    return total


def _load_timestamps(ts_path: str) -> list:
    with open(ts_path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise TTSWorkerError(ERR_ARTIFACT_INVALID, "timestamps no es una lista")
    return data


def _validate_timestamps(timestamps: list) -> None:
    if not timestamps:
        raise TTSWorkerError(ERR_ARTIFACT_INVALID, "timestamps vacío")
    last_end = -1.0
    for i, ts in enumerate(timestamps):
        if not isinstance(ts, dict):
            raise TTSWorkerError(ERR_ARTIFACT_INVALID, f"timestamp[{i}] no es objeto")
        try:
            start = float(ts.get("start_ms"))
            end = float(ts.get("end_ms"))
        except (TypeError, ValueError):
            raise TTSWorkerError(ERR_ARTIFACT_INVALID, f"timestamp[{i}] no numérico")
        if start < 0 or end < start:
            raise TTSWorkerError(
                ERR_ARTIFACT_INVALID,
                f"timestamp[{i}] inválido (start={start}, end={end})",
            )
        # Tolerancia mínima por redondeos de milisegundos en bloques contiguos.
        if end + 1.0 < last_end:
            raise TTSWorkerError(
                ERR_ARTIFACT_INVALID,
                f"timestamps no monótonos en [{i}] (end={end} < prev={last_end})",
            )
        last_end = max(last_end, end)
    if last_end <= 0:
        raise TTSWorkerError(ERR_ARTIFACT_INVALID, "timestamps sin duración")


def _validate_mp3(path: str, role: str) -> None:
    if not os.path.exists(path):
        raise TTSWorkerError(ERR_ARTIFACT_INVALID, f"{role} no existe: {path}")
    size = os.path.getsize(path)
    if size < MIN_MP3_BYTES:
        raise TTSWorkerError(
            ERR_ARTIFACT_INVALID, f"{role} demasiado pequeño ({size} < {MIN_MP3_BYTES})"
        )


def _validate_and_describe(base: str, cta: dict | None) -> dict:
    """Valida los artefactos y devuelve el bloque `files` para `estado.json`."""
    mp3_path = f"{base}.mp3"
    ts_path = f"{base}_timestamps.json"
    srt_path = f"{base}_subtitles.srt"

    _validate_mp3(mp3_path, "mp3")
    if not os.path.exists(ts_path):
        raise TTSWorkerError(ERR_ARTIFACT_INVALID, f"timestamps no existe: {ts_path}")
    timestamps = _load_timestamps(ts_path)
    _validate_timestamps(timestamps)

    files = {
        "mp3": _describe(mp3_path),
        "timestamps": _describe(
            ts_path, extra={"entries": len(timestamps), "last_end_ms": timestamps[-1].get("end_ms")}
        ),
    }
    if os.path.exists(srt_path):
        files["srt"] = _describe(srt_path)

    if cta:
        cta_mp3 = os.path.join(os.path.dirname(base), cta["mp3"])
        cta_ts = os.path.join(os.path.dirname(base), cta["timestamps"])
        _validate_mp3(cta_mp3, "cta_mp3")
        if not os.path.exists(cta_ts):
            raise TTSWorkerError(ERR_ARTIFACT_INVALID, f"cta timestamps no existe: {cta_ts}")
        files["cta_mp3"] = _describe(cta_mp3)
        files["cta_timestamps"] = _describe(
            cta_ts, extra={"entries": len(_load_timestamps(cta_ts))}
        )
    return files


def _env(key: str, default: str = "") -> str:
    return str(os.environ.get(key, default) or "").strip()


def run(request: dict, out_dir: str,
        engine_factory: Callable[[dict], Any] | None = None) -> dict:
    """Sintetiza el request y escribe artefactos + `estado.json`. Devuelve el estado.

    Lanza `TTSWorkerError` (con `code`) ante cualquier fallo; el llamante
    (`main`) lo traduce a un `estado.json` con `error_code` clasificado.
    """
    bloques = request.get("bloques")
    if not bloques:
        raise TTSWorkerError(ERR_REQUEST_INVALID, "request.bloques vacío")
    voice_config = request.get("voice_config") or {}
    base_name = str(request.get("output_base") or DEFAULT_BASE)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, os.path.splitext(base_name)[0])

    started_at = time.time()
    factory = engine_factory or _engine_factory
    try:
        engine = factory(voice_config)
    except Exception as e:  # noqa: BLE001
        raise TTSWorkerError(ERR_KOKORO_RUNTIME, f"engine init falló: {e}") from e

    t0 = time.time()
    try:
        audio_path, timestamps = engine.generate_segmented(bloques, output_path=base)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        code = ERR_NO_AUDIO if "no blocks produced audio" in msg.lower() else ERR_KOKORO_RUNTIME
        raise TTSWorkerError(code, msg) from e
    duration_s = (timestamps[-1].get("end_ms", 0) / 1000.0) if timestamps else 0.0

    # CTA opcional: mismo motor, sin volver a cargarlo. Fallo NON-FATAL (paridad
    # con la ruta local, donde el CTA fallido solo se avisa).
    cta_text = str(request.get("cta_text") or "").strip()
    cta = None
    cta_error = None
    if cta_text:
        cta_base = base + "_cta"
        try:
            cta_mp3, _ = engine.generate(cta_text, output_path=cta_base)
            cta = {
                "mp3": os.path.basename(cta_mp3),
                "timestamps": os.path.basename(f"{cta_base}_timestamps.json"),
            }
        except Exception as e:  # noqa: BLE001
            cta_error = str(e)

    files = _validate_and_describe(base, cta)

    estado = {
        "ok": True,
        "engine": "kokoro",
        "voice": getattr(engine, "kokoro_voice", None),
        "bloques": len(bloques),
        "words": _count_words(bloques),
        "artefactos": {
            "mp3": os.path.basename(f"{base}.mp3"),
            "timestamps": os.path.basename(f"{base}_timestamps.json"),
            "srt": os.path.basename(f"{base}_subtitles.srt"),
        },
        "cta": cta,
        "cta_error": cta_error,
        "duration_s": round(duration_s, 3),
        "elapsed_s": round(time.time() - t0, 2),
        "files": files,
        "node": _env("TAILDECK_NODE") or _env("HOSTNAME"),
        "image": _env("TAILDECK_AUTOTUBE_IMAGE"),
        "image_id": _env("TAILDECK_AUTOTUBE_IMAGE_ID"),
        "started_at": started_at,
        "finished_at": time.time(),
        "ts": time.time(),
        "rid": request.get("rid"),
    }
    with open(os.path.join(out_dir, "estado.json"), "w", encoding="utf-8") as fh:
        json.dump(estado, fh, ensure_ascii=False, indent=2)
    return estado


def _write_error_estado(out_dir: str, exc: Exception, started_at: float) -> str:
    """Escribe un estado de fallo clasificado. Devuelve el error_code usado."""
    code = exc.code if isinstance(exc, TTSWorkerError) else ERR_UNKNOWN
    payload = {
        "ok": False,
        "error": str(exc),
        "error_code": code,
        "node": _env("TAILDECK_NODE") or _env("HOSTNAME"),
        "image": _env("TAILDECK_AUTOTUBE_IMAGE"),
        "image_id": _env("TAILDECK_AUTOTUBE_IMAGE_ID"),
        "started_at": started_at,
        "finished_at": time.time(),
        "ts": time.time(),
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "estado.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TTS Kokoro sin DB (SuperServer)")
    ap.add_argument("--request", required=True, help="JSON de entrada")
    ap.add_argument("--out-dir", required=True, help="directorio de salida")
    args = ap.parse_args(argv)

    started_at = time.time()
    try:
        with open(args.request, encoding="utf-8") as fh:
            req = json.load(fh)
    except Exception as e:  # noqa: BLE001
        code = _write_error_estado(args.out_dir, TTSWorkerError(ERR_REQUEST_INVALID, str(e)), started_at)
        print(f"[tts-worker] ERROR ({code}): {e}", file=sys.stderr, flush=True)
        return 1

    try:
        estado = run(req, args.out_dir)
        print(f"[tts-worker] ok: {estado['artefactos']['mp3']} "
              f"({estado['bloques']} bloques, {estado['duration_s']}s, "
              f"node={estado.get('node') or '?'})", flush=True)
        return 0
    except Exception as e:  # noqa: BLE001 — cualquier fallo se reporta en estado.json
        code = _write_error_estado(args.out_dir, e, started_at)
        print(f"[tts-worker] ERROR ({code}): {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
