#!/usr/bin/env python3
"""Worker de render de UNA escena con el MISMO código que local (F5).

Ejecuta `VideoEditor._render_scene_segment` en el nodo, con el estado congelado
que el control plane calculó (`pipeline_dist/render_plan.py`): semilla por
escena, color grade, start_offset de vídeo y perfil Ken Burns de entrada.

Manifiesto (manifest.json, junto al asset en /work/in):
    {
      "config_module": "config.canal2_config",
      "seed_base": <int>,
      "index": <int>,
      "block_range": {...},
      "asset": {"path": "<abs control plane>", "type": "image|video", ...},
      "plan": {"start_offset": f, "color_grade": {...}|null, "last_kb": str|null}
    }

Salida en --out-dir: scene.mp4 + result.json (ok, sha256, filename, bytes).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Render de una escena (identity-preserving)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    manifest_path = os.path.abspath(args.manifest)
    in_dir = os.path.dirname(manifest_path)
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    # El código debe estar importable (horneado en la imagen o empujado en /app).
    for extra in (manifest.get("repo") or "", "/app"):
        if extra and extra not in sys.path:
            sys.path.insert(0, extra)
    os.environ.setdefault("DATABASE_PATH", "/tmp/autotube_worker.db")

    from pipeline.video_editor import VideoEditor

    if manifest.get("config"):
        # Config efectiva serializada por el control plane (incluye overrides de
        # test/profile y valores de DB) → misma que la del render local.
        from types import SimpleNamespace
        d = dict(manifest["config"])
        for _k in ("VIDEO_RESOLUTION", "VIDEO_SIZE"):
            if isinstance(d.get(_k), list):
                d[_k] = tuple(d[_k])
        cfg = SimpleNamespace(**d)
    else:
        cfg = importlib.import_module(str(manifest.get("config_module") or "config.canal2_config"))
    ve = VideoEditor(cfg)

    # ── Estado congelado por el control plane ──
    ve._video_seed = int(manifest.get("seed_base", 0) or 0)
    plan = manifest.get("plan") or {}
    # La escena que DEFINE el grade debe computarlo (mismo consumo de RNG que
    # local); el resto usa el grade congelado.
    ve._video_color_grade = None if plan.get("sets_color_grade") else plan.get("color_grade")
    asset = dict(manifest.get("asset") or {})
    orig_path = str(asset.get("path") or "")
    # El asset se empuja junto al manifiesto: reescribimos su ruta al fichero local.
    if orig_path:
        asset["path"] = os.path.join(in_dir, os.path.basename(orig_path))
    p = str(asset.get("path") or "")
    ve._video_offset_tracker = {p: float(plan.get("start_offset", 0.0))} if p else {}
    ve._last_ken_burns_profile = plan.get("last_kb")
    ve._current_clip_idx = int(manifest.get("index", 0) or 0)
    ve._on_demand_fetcher = None
    ve._pending_fill_dur = 0.0
    ve._pending_fill_usable = 0.0

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, "scene.mp4")
    # Limpia salidas previas: evita el sufijo de colisión de `_render_scene_segment`
    # (que renombraría el fichero) y garantiza que la salida sea scene.mp4.
    import glob as _glob
    for _f in _glob.glob(os.path.join(args.out_dir, "scene*.mp4")):
        try:
            os.remove(_f)
        except OSError:
            pass

    result = {"ok": False}
    try:
        res = ve._render_scene_segment(
            manifest.get("block_range") or {},
            asset,
            out_path,
            int(manifest.get("index", 0) or 0),
            [],
        )
        produced = str(res) if res else ""
        if produced and os.path.abspath(produced) != os.path.abspath(out_path) \
                and os.path.exists(produced):
            os.replace(produced, out_path)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            result = {
                "ok": True,
                "filename": "scene.mp4",
                "sha256": _sha256(out_path),
                "bytes": os.path.getsize(out_path),
            }
        else:
            result = {"ok": False, "error": "render devolvió None o fichero vacío"}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}

    with open(os.path.join(args.out_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
