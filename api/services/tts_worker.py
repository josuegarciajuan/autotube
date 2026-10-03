#!/usr/bin/env python3
"""TTS Kokoro worker SIN base de datos (adaptación SuperServer, opción A).

Unidad de pool pura: recibe un request JSON, sintetiza con Kokoro y deja los
artefactos en `--out-dir`. NO toca `autotube.db`; la casa es la única que aplica
el resultado (ruta de audio + timestamps) a la DB.

Contrato de entrada (`--request`):
    {
      "bloques": [ {"tipo": "...", "texto": "...", ...}, ... ],   # obligatorio
      "voice_config": { "kokoro_voice": "em_santa", ... },        # opcional
      "output_base": "narration_<videoId>"                        # opcional
    }

Salida en `--out-dir`:
    <base>.mp3, <base>_timestamps.json, <base>_subtitles.srt, estado.json

`estado.json` (lo que la casa usa para aplicar y verificar):
    { ok, engine, voice, bloques, duration_s, elapsed_s,
      artefactos: {mp3, timestamps, srt}, ts }

Uso:
    python3 api/services/tts_worker.py --request in/request.json --out-dir out
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable

DEFAULT_BASE = "narration"


def _engine_factory(voice_config: dict):
    """Import diferido para no cargar torch/kokoro si solo se testea la lógica."""
    from pipeline.kokoro_tts import KokoroTTSEngine

    return KokoroTTSEngine(voice_config)


def _estado_ok(engine, bloques: int, base: str, elapsed_s: float) -> dict:
    return {
        "ok": True,
        "engine": "kokoro",
        "voice": getattr(engine, "kokoro_voice", None),
        "bloques": bloques,
        "artefactos": {
            "mp3": os.path.basename(f"{base}.mp3"),
            "timestamps": os.path.basename(f"{base}_timestamps.json"),
            "srt": os.path.basename(f"{base}_subtitles.srt"),
        },
        "elapsed_s": round(elapsed_s, 2),
        "ts": time.time(),
    }


def run(request: dict, out_dir: str,
        engine_factory: Callable[[dict], Any] | None = None) -> dict:
    """Sintetiza el request y escribe artefactos + `estado.json`. Devuelve el estado."""
    bloques = request.get("bloques")
    if not bloques:
        raise ValueError("request.bloques vacío")
    voice_config = request.get("voice_config") or {}
    base_name = str(request.get("output_base") or DEFAULT_BASE)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, os.path.splitext(base_name)[0])

    factory = engine_factory or _engine_factory
    engine = factory(voice_config)

    t0 = time.time()
    audio_path, timestamps = engine.generate_segmented(bloques, output_path=base)
    duration_s = (timestamps[-1].get("end_ms", 0) / 1000.0) if timestamps else 0.0

    # CTA opcional: mismo motor, sin volver a cargarlo (se apaga después).
    cta_text = str(request.get("cta_text") or "").strip()
    cta = None
    if cta_text:
        cta_base = base + "_cta"
        cta_mp3, _ = engine.generate(cta_text, output_path=cta_base)
        cta = {
            "mp3": os.path.basename(cta_mp3),
            "timestamps": os.path.basename(f"{cta_base}_timestamps.json"),
        }

    estado = _estado_ok(engine, len(bloques), base, time.time() - t0)
    estado["duration_s"] = round(duration_s, 3)
    estado["rid"] = request.get("rid")
    estado["cta"] = cta
    with open(os.path.join(out_dir, "estado.json"), "w", encoding="utf-8") as fh:
        json.dump(estado, fh, ensure_ascii=False, indent=2)
    return estado


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TTS Kokoro sin DB (SuperServer)")
    ap.add_argument("--request", required=True, help="JSON de entrada")
    ap.add_argument("--out-dir", required=True, help="directorio de salida")
    args = ap.parse_args(argv)

    try:
        with open(args.request, encoding="utf-8") as fh:
            req = json.load(fh)
        estado = run(req, args.out_dir)
        print(f"[tts-worker] ok: {estado['artefactos']['mp3']} "
              f"({estado['bloques']} bloques, {estado['duration_s']}s)", flush=True)
        return 0
    except Exception as e:  # noqa: BLE001 — cualquier fallo se reporta en estado.json
        os.makedirs(args.out_dir, exist_ok=True)
        with open(os.path.join(args.out_dir, "estado.json"), "w", encoding="utf-8") as fh:
            json.dump({"ok": False, "error": str(e), "ts": time.time()}, fh,
                      ensure_ascii=False, indent=2)
        print(f"[tts-worker] ERROR: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
