#!/usr/bin/env python3
"""Worker de render de UNA escena (se ejecuta EN el nodo, sin DB ni red).

Uso (dentro del contenedor, con el manifiesto empujado a /work/in):
    python /app/pipeline_dist/scene_worker.py \
        --manifest /work/in/manifest.json --out-dir /work/out

Salida en `out-dir`:
    scene.mp4    segmento visual (video-only, sin audio)
    result.json  {ok, sha256, frames, duration, fps, ...} verificable

El manifiesto trae TODAS las decisiones (asset, duración, frames, zoom, color,
encoding). Este worker no elige nada ni toca la base de datos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from typing import Optional


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _manifest_hash(manifest: dict) -> str:
    blob = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _probe(path: str) -> dict:
    """ffprobe mínimo; si falla, devuelve {} y el resultado usa los valores del plan."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return {}
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or [{}]
        st = streams[0]
        info = {"width": st.get("width"), "height": st.get("height")}
        if st.get("nb_read_frames"):
            try:
                info["frames"] = int(st["nb_read_frames"])
            except (TypeError, ValueError):
                pass
        rate = st.get("r_frame_rate") or ""
        if "/" in rate:
            num, den = rate.split("/", 1)
            try:
                den_f = float(den)
                if den_f:
                    info["fps"] = round(float(num) / den_f, 6)
            except (TypeError, ValueError):
                pass
        dur = (data.get("format") or {}).get("duration")
        if dur:
            try:
                info["duration"] = round(float(dur), 6)
            except (TypeError, ValueError):
                pass
        return info
    except Exception:
        return {}


def _eq_filter(color: dict) -> str:
    contrast = float(color.get("contrast", 1.0))
    brightness = float(color.get("brightness", 1.0))
    saturation = float(color.get("saturation", 1.0))
    if abs(contrast - 1) < 1e-4 and abs(brightness - 1) < 1e-4 and abs(saturation - 1) < 1e-4:
        return ""
    return f"eq=contrast={contrast:.4f}:brightness={brightness:.4f}:saturation={saturation:.4f}"


def _base_vf(manifest: dict) -> list[str]:
    w = int(manifest["width"])
    h = int(manifest["height"])
    fps = float(manifest["fps"])
    parts = [
        f"scale={w}:{h}:force_original_aspect_ratio=increase",
        f"crop={w}:{h}",
        "setsar=1",
        f"fps={fps}",
    ]
    return parts


def _build_command(manifest: dict, in_dir: str, out_path: str) -> list[str]:
    asset = manifest.get("asset") or {}
    kind = str(asset.get("kind", "test"))
    frames = int(manifest["frames"])
    fps = float(manifest["fps"])
    w, h = int(manifest["width"]), int(manifest["height"])
    enc = manifest.get("encoding") or {}
    codec = str(enc.get("codec", "libx264"))
    crf = str(enc.get("crf", 16))
    preset = str(enc.get("preset", "veryfast"))
    pix_fmt = str(enc.get("pix_fmt", "yuv420p"))
    eq = _eq_filter(manifest.get("color") or {})

    common_tail = [
        "-frames:v", str(frames),
        "-an",
        "-c:v", codec,
        "-crf", crf,
        "-preset", preset,
        "-pix_fmt", pix_fmt,
        "-movflags", "+faststart",
        out_path,
    ]

    if kind == "test":
        # Color válido 0xRRGGBB determinista por escena (para smoke/placeholder).
        idx = int(manifest.get("index", 0)) & 0xFF
        color = "0x%02x%02x%02x" % (0x10 + (idx & 0x3F), 0x20 + ((idx * 3) & 0x3F), 0x30 + ((idx * 7) & 0x3F))
        vf = ",".join(_base_vf(manifest) + ([eq, "format=yuv420p"] if eq else ["format=yuv420p"]))
        return [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={fps}",
            "-t", f"{frames / fps:.6f}",
            "-vf", vf,
            *common_tail,
        ]

    if kind == "video":
        src = os.path.join(in_dir, str(asset.get("path", "")))
        trim = float(asset.get("trim_start", 0.0) or 0.0)
        vf = ",".join(_base_vf(manifest) + ([eq] if eq else []))
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if trim > 0:
            cmd += ["-ss", f"{trim:.6f}"]
        cmd += ["-i", src, "-vf", vf, *common_tail]
        return cmd

    # image (default)
    src = os.path.join(in_dir, str(asset.get("path", "")))
    motion = manifest.get("motion") or {}
    z0 = float(motion.get("zoom_start", 1.0))
    z1 = float(motion.get("zoom_end", 1.0))
    if abs(z1 - z0) > 1e-4:
        step = (z1 - z0) / max(1, frames - 1)
        zoompan = (
            f"zoompan=z='min({z0:.4f}+{step:.6f}*on,{z1:.4f})'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={w}x{h}:fps={fps}"
        )
        vf_parts = [
            f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase",
            f"crop={w * 2}:{h * 2}",
            zoompan,
            "setsar=1",
        ]
        if eq:
            vf_parts.append(eq)
        vf_parts.append("format=yuv420p")
        return [
            "ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", src,
            "-vf", ",".join(vf_parts),
            *common_tail,
        ]
    vf = ",".join(_base_vf(manifest) + ([eq, "format=yuv420p"] if eq else ["format=yuv420p"]))
    return [
        "ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", src,
        "-t", f"{frames / fps:.6f}",
        "-vf", vf,
        *common_tail,
    ]


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"ffmpeg rc={proc.returncode}: {tail}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Render de una escena (Autotube distribuido)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "scene.mp4")
    result_path = os.path.join(out_dir, "result.json")

    t0 = time.time()
    try:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        _write_result(result_path, {"ok": False, "error": f"manifest ilegible: {exc}"})
        print(f"[scene_worker] manifest ilegible: {exc}", file=sys.stderr)
        return 2

    scene_key = str(manifest.get("scene_key", "?"))
    in_dir = os.path.dirname(os.path.abspath(args.manifest))
    frames = int(manifest.get("frames", 0))
    fps = float(manifest.get("fps", 30))

    if frames <= 0 or fps <= 0:
        _write_result(result_path, {"ok": False, "scene_key": scene_key,
                                    "error": "frames/fps invalidos"})
        return 2

    cmd = _build_command(manifest, in_dir, out_path)
    try:
        _run(cmd)
        if not os.path.exists(out_path) or os.path.getsize(out_path) <= 1024:
            raise RuntimeError("la salida no existe o es demasiado pequena")
    except Exception as exc:
        # Reintento conservador para imagenes animadas (zoompan fragil).
        asset = manifest.get("asset") or {}
        motion = manifest.get("motion") or {}
        retried = False
        if str(asset.get("kind", "")) == "image" and abs(
            float(motion.get("zoom_end", 1.0)) - float(motion.get("zoom_start", 1.0))
        ) > 1e-4:
            manifest["motion"] = {"zoom_start": 1.0, "zoom_end": 1.0, "focus": "center"}
            retried = True
            try:
                _run(_build_command(manifest, in_dir, out_path))
            except Exception as exc2:
                _write_result(result_path, {"ok": False, "scene_key": scene_key,
                                            "error": f"render fallido: {exc2}"})
                print(f"[scene_worker] {scene_key} render fallido: {exc2}", file=sys.stderr)
                return 2
        if not retried and (not os.path.exists(out_path) or os.path.getsize(out_path) <= 1024):
            _write_result(result_path, {"ok": False, "scene_key": scene_key,
                                        "error": f"render fallido: {exc}"})
            print(f"[scene_worker] {scene_key} render fallido: {exc}", file=sys.stderr)
            return 2

    probe = _probe(out_path)
    result = {
        "ok": True,
        "scene_key": scene_key,
        "filename": "scene.mp4",
        "sha256": _sha256(out_path),
        "frames": probe.get("frames", frames),
        "duration": probe.get("duration", round(frames / fps, 6)),
        "fps": probe.get("fps", fps),
        "width": probe.get("width", manifest.get("width")),
        "height": probe.get("height", manifest.get("height")),
        "manifest_hash": _manifest_hash(manifest),
        "render_ms": int((time.time() - t0) * 1000),
    }
    _write_result(result_path, result)
    print(f"[scene_worker] {scene_key} ok frames={result['frames']} "
          f"dur={result['duration']} ({result['render_ms']} ms)")
    return 0


def _write_result(path: str, obj: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


if __name__ == "__main__":
    raise SystemExit(main())
