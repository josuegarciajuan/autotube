#!/usr/bin/env python3
"""Worker de generación de UNA imagen IA (Local SD 1.5 CPU) en la flota.

Ejecuta el MISMO `LocalSDProvider` de la casa dentro del contenedor hermético
`autotube-sd:1`, con la petición congelada por el coordinador. Sin DB, sin
secretos, sin red (`--network=none`): el modelo SD y los pesos del upscaler se
montan/hornean en el nodo.

Petición (request.json, junto a este worker en /work/in):
    {
      "key": "0000",
      "index": 0,
      "prompt": "...",
      "negative_prompt": "...",
      "seed": <int|null>,
      "width": 768, "height": 432,
      "steps": 8,
      "output": "scene_0000.jpg",
      "model_id": "runwayml/stable-diffusion-v1-5",
      "upscale_min": [1920, 1080] | null,
      "upscale_model": "espcn",
      "upscale_sharpen": true,
      "upscale_sharpen_amount": 0.7,
      "upscale_sharpen_sigma": 1.4
    }

Salida en --out-dir: <output>.jpg + result.json (ok, filename, sha256, bytes,
width, height, seed, steps, provider, elapsed_ms) o `ok:false` + error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _tune_threads() -> None:
    """Acota torch al cpuQuota de la unidad (evita oversubscription por nodo).

    En nodos-contenedor estrictos, crear hilos puede fallar; en ese caso se
    degrada a 1 hilo en vez de tumbar la unidad (el solicitante puede además
    fijar `threads=1` por params).
    """
    threads = int(os.environ.get("SD_THREADS", "0") or 0)
    if threads <= 0:
        return
    try:
        import torch
        torch.set_num_threads(threads)
    except Exception:  # noqa: BLE001
        pass


def _ensure_upscale_model() -> None:
    """Copia el ESPCN_x2 horneado a `$OUTPUT_DIR/models` (sin red en runtime).

    `AIImageUpscaler` busca el modelo en `settings.OUTPUT_DIR/models`. En el
    contenedor no hay red (`--network=none`), así que se provee desde
    `/opt/upscale/espcn_x2.pb`. Si no está, el upscaler degrada a no-op y la
    imagen se devuelve a resolución nativa (el pipeline nunca se rompe).
    """
    try:
        import shutil
        from config import settings
        model_dir = Path(settings.OUTPUT_DIR) / "models"
        model_dir.mkdir(parents=True, exist_ok=True)
        src = Path("/opt/upscale/espcn_x2.pb")
        dst = model_dir / "espcn_x2.pb"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Imagen IA (Local SD 1.5) para el pool")
    ap.add_argument("--request", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    with open(args.request, encoding="utf-8") as fh:
        req = json.load(fh)

    # El código debe estar importable (horneado en la imagen o empujado en /app).
    for extra in (os.environ.get("AUTOTUBE_CODE_DIR") or "", "/app"):
        if extra and extra not in sys.path:
            sys.path.insert(0, extra)

    _tune_threads()
    _ensure_upscale_model()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index = int(req.get("index", 0) or 0)
    filename = str(req.get("output") or f"scene_{index:04d}.jpg")
    out_path = out_dir / filename

    result: dict = {"ok": False, "key": req.get("key"), "filename": filename}
    try:
        from pipeline.providers.local_sd_provider import LocalSDProvider

        upscale_min = req.get("upscale_min")
        provider = LocalSDProvider(
            num_inference_steps=int(req.get("steps", 8) or 8),
            model_id=req.get("model_id") or None,
            width=int(req.get("width", 768) or 768),
            height=int(req.get("height", 432) or 432),
            upscale_min=tuple(upscale_min) if upscale_min and len(upscale_min) == 2 else None,
            upscale_model=req.get("upscale_model") or None,
            upscale_sharpen=bool(req.get("upscale_sharpen", True)),
            upscale_sharpen_amount=float(req.get("upscale_sharpen_amount", 0.4)),
            upscale_sharpen_sigma=float(req.get("upscale_sharpen_sigma", 2.0)),
        )

        t0 = time.monotonic()
        produced = provider.generate(
            prompt=str(req.get("prompt") or ""),
            output_path=out_path,
            seed=req.get("seed"),
            negative_prompt=str(req.get("negative_prompt") or ""),
        )
        if produced and Path(produced).exists() and Path(produced).stat().st_size > 1024:
            width = height = None
            try:
                from PIL import Image
                with Image.open(produced) as im:
                    width, height = im.size
            except Exception:  # noqa: BLE001
                pass
            result.update({
                "ok": True,
                "filename": Path(produced).name,
                "sha256": _sha256(str(produced)),
                "bytes": Path(produced).stat().st_size,
                "width": width,
                "height": height,
                "seed": req.get("seed"),
                "steps": req.get("steps"),
                "provider": "local_sd",
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
            })
        else:
            result["error"] = "generate devolvió None o imagen vacía"
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)

    with open(out_dir / "result.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
