#!/usr/bin/env python3
"""Worker de render de SHORTS para la flota SuperServer (sin DB, offline).

Reutiliza EXACTAMENTE `pipeline.shorts_media.render_short_hybrid` (el mismo que
la casa) dentro del contenedor de render, de modo que la salida es idéntica por
construcción. La casa prepara los insumos (assets + audio + srt) y un `spec.json`
con las rutas RELATIVAS a `/work/in`; este worker las resuelve, renderiza y deja
`out/render.mp4` + `out/result.json` verificable.

Contrato:
  python3 /app/pipeline_dist/short_render_worker.py --spec /work/in/spec.json --out-dir /work/out

spec.json:
  {
    "assets": [ {"path": "in/a0.mp4", "type": "video"|"image"}, ... ],
    "audio": "in/audio.mp3",
    "srt": "in/sub.srt" | null,
    "audio_duration": 45.2 | null,
    "scene_ranges": [ {"duration": 5.0}, ... ] | null,
    "crossfade_dur": 1.0,
    "bg_color_hex": "0a0a1a",
    "output": "out/render.mp4"
  }
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

WORK = Path("/work")


def _resolve(rel: str) -> Path:
    """Resuelve una ruta relativa al workdir (/work); acepta absolutas tal cual."""
    p = Path(str(rel))
    return p if p.is_absolute() else (WORK / p)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="/work/in/spec.json")
    ap.add_argument("--out-dir", default="/work/out")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"ok": False}

    try:
        with open(args.spec, encoding="utf-8") as fh:
            spec = json.load(fh)

        # Código de autotube montado en /app (ro). El worker reutiliza el mismo
        # módulo de render que la casa.
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")
        from pipeline.shorts_media import render_short_hybrid  # noqa: E402

        assets = []
        for a in spec.get("assets") or []:
            if not a:
                assets.append(None)
                continue
            path = _resolve(a.get("path"))
            assets.append({"path": path, "type": a.get("type", "image")})

        audio = _resolve(spec.get("audio"))
        srt_rel = spec.get("srt")
        srt = _resolve(srt_rel) if srt_rel else None
        output = _resolve(spec.get("output") or "out/render.mp4")
        output.parent.mkdir(parents=True, exist_ok=True)

        render_short_hybrid(
            asset_items=assets,
            audio_path=audio,
            output_path=output,
            audio_duration=spec.get("audio_duration"),
            bg_color_hex=spec.get("bg_color_hex", "0a0a1a"),
            crossfade_dur=float(spec.get("crossfade_dur", 1.0) or 1.0),
            srt_path=srt,
            scene_ranges=spec.get("scene_ranges") or None,
        )

        if not output.exists() or output.stat().st_size <= 0:
            raise RuntimeError("render_short_hybrid no produjo salida")

        result = {
            "ok": True,
            "filename": output.name,
            "sha256": _sha256(output),
            "bytes": output.stat().st_size,
        }
    except Exception as exc:  # noqa: BLE001 — el fallo se reporta, no se propaga
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    with open(out_dir / "result.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
