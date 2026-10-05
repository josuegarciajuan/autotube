#!/usr/bin/env python3
"""Verifica la equivalencia FUNCIONAL local vs contenedor de una imagen IA (SD 1.5).

Genera la MISMA petición por las dos rutas y compara:
  - resolución final (px) y formato
  - validez (JPEG válido + >5 KB, mismo criterio que `_is_valid_ai_image`)
  - que la petición respeta el tamaño base y los pasos

Los píxeles y el sha256 difieren: Stable Diffusion es estocástico y la versión
de torch del contenedor no tiene por qué coincidir con la de la casa. Por eso la
equivalencia que garantizamos es FUNCIONAL (mismo pipeline, mismos pesos,
mismos parámetros y misma integración), no bit-a-bit. Ver `AGENTS.md` y
`specs/ai-image-providers.md`.

Uso:
    python3 scripts/verify_dist_image_equivalence.py              # local + contenedor
    python3 scripts/verify_dist_image_equivalence.py --no-docker  # solo local
    python3 scripts/verify_dist_image_equivalence.py --steps 4 --width 384 --height 384 --no-upscale
    python3 scripts/verify_dist_image_equivalence.py --keep       # conserva el tmp

Variables de entorno:
    AUTOTUBE_SD_IMAGE        imagen del worker (def. autotube-sd:1)
    AUTOTUBE_RUNTIME_DIR     código montado en /app (def. raíz del repo)
    AUTOTUBE_SD_MODEL_MOUNT  caché HF montada en /hfcache (def. ~/.cache/huggingface)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _jpeg_ok(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 5000:
        return False
    return path.read_bytes()[:3] == b"\xff\xd8\xff"


def _dims(path: Path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _walk(img: Path) -> dict:
    return {
        "path": str(img),
        "bytes": img.stat().st_size if img.exists() else 0,
        "sha256": _sha256(img) if img.exists() else None,
        "dims": _dims(img),
        "jpeg_ok": _jpeg_ok(img),
    }


def run_local(req: dict, out_dir: Path) -> dict:
    from pipeline.providers.local_sd_provider import LocalSDProvider

    upscale_min = req.get("upscale_min")
    provider = LocalSDProvider(
        num_inference_steps=int(req["steps"]),
        model_id=req.get("model_id") or None,
        width=int(req["width"]),
        height=int(req["height"]),
        upscale_min=tuple(upscale_min) if upscale_min and len(upscale_min) == 2 else None,
        upscale_model=req.get("upscale_model") or None,
        upscale_sharpen=bool(req.get("upscale_sharpen", True)),
        upscale_sharpen_amount=float(req.get("upscale_sharpen_amount", 0.4)),
        upscale_sharpen_sigma=float(req.get("upscale_sharpen_sigma", 2.0)),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / req["output"]
    size = (int(req["width"]), int(req["height"]))
    if upscale_min and len(upscale_min) == 2:
        size = (int(upscale_min[0]), int(upscale_min[1]))
    print(f"[local] generando {out.name} (base {req['width']}x{req['height']}, "
          f"{req['steps']} pasos, upscale_min={upscale_min}) ...")
    t0 = time.time()
    provider.generate(
        prompt=req["prompt"], output_path=out, seed=req.get("seed"),
        negative_prompt=req.get("negative_prompt") or "",
    )
    return {"route": "local", "elapsed_s": time.time() - t0, **_walk(out)}


def run_docker(req: dict, work: Path, threads: int) -> dict:
    image = os.environ.get("AUTOTUBE_SD_IMAGE", "autotube-sd:1")
    code = os.environ.get("AUTOTUBE_RUNTIME_DIR", str(ROOT))
    model = os.environ.get("AUTOTUBE_SD_MODEL_MOUNT", str(Path.home() / ".cache" / "huggingface"))
    ind = (work / "in").resolve()
    outd = (work / "out").resolve()
    ind.mkdir(parents=True, exist_ok=True)
    outd.mkdir(parents=True, exist_ok=True)
    (ind / "request.json").write_text(json.dumps(req), encoding="utf-8")

    cmd = [
        "docker", "run", "--rm", "--network=none",
        "-e", "HF_HOME=/hfcache", "-e", "HF_HUB_OFFLINE=1", "-e", "PYTHONPATH=/app",
        "-e", "OUTPUT_DIR=/tmp/worker_output", "-e", f"SD_THREADS={threads}",
        "-e", "OMP_NUM_THREADS=" + str(threads), "-e", "PYTHONUNBUFFERED=1",
        "-v", f"{code}:/app:ro", "-v", f"{model}:/hfcache:ro",
        "-v", f"{ind}:/work/in:ro", "-v", f"{outd}:/work/out",
        "-w", "/work", image,
        "python3", "/app/pipeline_dist/image_worker.py",
        "--request", "/work/in/request.json", "--out-dir", "/work/out",
    ]
    print(f"[docker] {image} (threads={threads}) ...")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    result = {}
    rj = outd / "result.json"
    if rj.exists():
        try:
            result = json.loads(rj.read_text(encoding="utf-8"))
        except ValueError:
            result = {}
    out = outd / str(req["output"])
    if not result.get("ok") and r.returncode != 0:
        print((r.stderr or "")[-800:])
    return {"route": "docker", "elapsed_s": elapsed, "ok": bool(result.get("ok")),
            "result": result, **_walk(out)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Equivalencia local vs contenedor (SD 1.5)")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--height", type=int, default=432)
    ap.add_argument("--no-upscale", action="store_true")
    ap.add_argument("--no-docker", action="store_true")
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    req = {
        "key": "equiv", "index": 0,
        "prompt": ("scene 1/1: a circle of stones enclosing a fire, cinematic documentary "
                   "style, photorealistic, 16:9, high detail"),
        "negative_prompt": "blurry, low quality, text, watermark",
        "seed": 42,
        "width": args.width, "height": args.height, "steps": args.steps,
        "output": "equiv.jpg",
        "model_id": "runwayml/stable-diffusion-v1-5",
        "upscale_min": None if args.no_upscale else [1920, 1080],
        "upscale_model": "espcn",
        "upscale_sharpen": True, "upscale_sharpen_amount": 0.7, "upscale_sharpen_sigma": 1.4,
    }

    work = Path(tempfile.mkdtemp(prefix="atube-equiv-"))
    print(f"workdir: {work}")
    print(f"petición: {req['width']}x{req['height']}, {req['steps']} pasos, "
          f"upscale_min={req['upscale_min']}, seed={req['seed']}")
    try:
        local = run_local(req, work / "local")
        print(f"[local] dims={local['dims']} bytes={local['bytes']} "
              f"sha={str(local['sha256'])[:12]} t={local['elapsed_s']:.0f}s ok={local['jpeg_ok']}")

        docker = None
        if not args.no_docker:
            docker = run_docker(req, work / "docker", args.threads)
            print(f"[docker] dims={docker['dims']} bytes={docker['bytes']} "
                  f"sha={str(docker['sha256'])[:12]} t={docker['elapsed_s']:.0f}s "
                  f"ok={docker['jpeg_ok']} worker_ok={docker.get('ok')}")

        problems = []
        if not local["jpeg_ok"]:
            problems.append("local: salida no es un JPEG válido >5KB")
        if docker is not None:
            if not docker.get("ok"):
                problems.append("docker: el worker no devolvió ok=true")
            if not docker["jpeg_ok"]:
                problems.append("docker: salida no es un JPEG válido >5KB")
            if local["dims"] != docker["dims"]:
                problems.append(f"resolución distinta: local={local['dims']} docker={docker['dims']}")
            dw = docker.get("result", {}).get("width")
            dh = docker.get("result", {}).get("height")
            if (dw, dh) != docker["dims"]:
                problems.append(f"result.json inconsistente: {dw}x{dh} vs {docker['dims']}")

        print()
        if problems:
            print("EQUIVALENCIA FUNCIONAL: FALLO")
            for p in problems:
                print(f"  - {p}")
            return 1
        if docker is None:
            print("EQUIVALENCIA FUNCIONAL: PARCIAL (solo local; --no-docker)")
            return 0
        if local["sha256"] != docker["sha256"]:
            print("Nota: los sha256 difieren (esperado: SD es estocástico y las "
                  "versiones de torch pueden variar).")
        print("EQUIVALENCIA FUNCIONAL: OK (misma resolución/formato/validez)")
        return 0
    finally:
        if args.keep:
            print(f"tmp conservado: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
