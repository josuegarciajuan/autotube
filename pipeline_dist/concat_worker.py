#!/usr/bin/env python3
"""Worker de concat por batch (se ejecuta EN el nodo, sin DB ni red).

Contrato de identidad: NO decide nada. El control plane ya calculó el
``filter_complex`` y los parámetros exactos de encoding que usa el editor local,
y se los pasa en ``spec.json``. Este worker solo ejecuta ffmpeg y publica
``result.json`` verificable (sha256/bytes).

Uso (dentro del contenedor autotube-worker:1):
    python /app/pipeline_dist/concat_worker.py \
        --spec /work/in/spec.json --out-dir /work/out

spec.json:
    {
      "inputs": ["scene_0000.mp4", ...],   # basenames en el dir del spec
      "filter_complex": "...",
      "map_label": "[final]",
      "codec": "libx264", "preset": "fast", "bitrate": "6000k",
      "pix_fmt": "yuv420p",
      "output": "batch_0000.mp4",
      "timeout": 1800
    }
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Concat de un batch (ffmpeg, identity-preserving)")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)

    in_dir = os.path.dirname(os.path.abspath(args.spec))
    os.makedirs(args.out_dir, exist_ok=True)
    out_name = str(spec.get("output") or "batch.mp4")
    out_path = os.path.join(args.out_dir, out_name)

    cmd = ["ffmpeg", "-y", "-v", "error"]
    for name in spec.get("inputs") or []:
        cmd += ["-i", os.path.join(in_dir, str(name))]
    cmd += [
        "-filter_complex", str(spec["filter_complex"]),
        "-map", str(spec.get("map_label") or "[final]"),
        "-c:v", str(spec.get("codec") or "libx264"),
        "-preset", str(spec.get("preset") or "fast"),
        "-b:v", str(spec.get("bitrate") or "6000k"),
        "-pix_fmt", str(spec.get("pix_fmt") or "yuv420p"),
        "-an",
        "-movflags", "+faststart",
        out_path,
    ]

    result = {"ok": False}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=int(spec.get("timeout") or 1800))
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            result = {
                "ok": True,
                "filename": out_name,
                "sha256": _sha256(out_path),
                "bytes": os.path.getsize(out_path),
                "segments": len(spec.get("inputs") or []),
            }
        else:
            result = {"ok": False, "error": (r.stderr or "")[-600:]}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}

    with open(os.path.join(args.out_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
