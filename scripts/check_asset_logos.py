#!/usr/bin/env python3
"""Fase 1 — Detector read-only de overlays/marcas (logos) en las 4 esquinas.

Para cada asset de imagen (o frame representativo de un vídeo) calcula la
densidad de bordes y la varianza de cada esquina y marca como sospechosa la
que destaca anómalamente frente a las demás. Sirve para identificar el asset y
el proveedor exactos de una marca/watermark.

NO borra ni modifica ningún archivo. Solo lee.

Dependencias: PIL (obligatoria). numpy es opcional — si falta, se degrada a un
detector de bordes de PIL (menos preciso). No añade dependencias nuevas.

Usage:
    python3 scripts/check_asset_logos.py output/images/foo.jpg
    python3 scripts/check_asset_logos.py output/images/ --ratio 2.0 --json
    python3 scripts/check_asset_logos.py --name pixabay_photo_3732269
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from statistics import median

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("check_asset_logos")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}

CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


def _numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def _edge_image(gray):
    """Return (edge_image, mode). numpy gradient if available, else PIL."""
    try:
        import numpy as np
        from PIL import Image

        arr = np.asarray(gray, dtype=np.float32)
        gy, gx = np.gradient(arr)
        mag = np.hypot(gx, gy)
        # Normalize to a 0-255 range using the 99th percentile (robust to a
        # single hard edge) so comparisons are stable across images.
        if mag.size:
            p99 = float(np.percentile(mag, 99.0))
            if p99 > 1e-6:
                mag = mag / p99 * 255.0
        mag = np.clip(mag, 0, 255).astype("uint8")
        return Image.fromarray(mag), "numpy"
    except Exception:
        from PIL import ImageFilter
        return gray.filter(ImageFilter.FIND_EDGES), "pil"


def _corner_boxes(w: int, h: int, frac: float) -> dict[str, tuple[int, int, int, int]]:
    cw = max(8, int(w * frac))
    ch = max(8, int(h * frac))
    return {
        "top-left": (0, 0, cw, ch),
        "top-right": (w - cw, 0, w, ch),
        "bottom-left": (0, h - ch, cw, h),
        "bottom-right": (w - cw, h - ch, w, h),
    }


def analyze_image(path: Path, frac: float, ratio_threshold: float,
                  edge_floor: float, std_floor: float) -> dict:
    """Analyse one image; returns a report dict. Never raises for IO issues."""
    from PIL import Image, ImageStat

    report: dict = {"path": str(path), "ok": False, "corners": {}}
    try:
        with Image.open(path) as img:
            img = img.convert("L")
            w, h = img.size
            if w < 32 or h < 32:
                report["error"] = "image too small to inspect"
                return report
            edge, mode = _edge_image(img)
            boxes = _corner_boxes(w, h, frac)
            metrics: dict[str, dict] = {}
            for name, box in boxes.items():
                edge_patch = edge.crop(box)
                gray_patch = img.crop(box)
                edge_mean = float(ImageStat.Stat(edge_patch).mean[0])
                gray_std = float(ImageStat.Stat(gray_patch).stddev[0])
                metrics[name] = {
                    "edge_mean": round(edge_mean, 2),
                    "std": round(gray_std, 2),
                }
            edge_vals = [m["edge_mean"] for m in metrics.values()]
            med = median(edge_vals) if edge_vals else 0.0
            suspicious = []
            for name, m in metrics.items():
                ratio = m["edge_mean"] / (med + 1e-6)
                m["ratio_vs_median"] = round(ratio, 2)
                m["suspicious"] = bool(
                    ratio >= ratio_threshold
                    and m["edge_mean"] >= edge_floor
                    and m["std"] >= std_floor
                )
                if m["suspicious"]:
                    suspicious.append(name)
            report.update({
                "ok": True,
                "width": w,
                "height": h,
                "mode": mode,
                "median_corner_edge": round(med, 2),
                "suspicious_corners": suspicious,
                "corners": metrics,
            })
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


def extract_video_frame(path: Path, tmp_dir: Path) -> Path | None:
    """Extract a representative frame (read-only w.r.t. the asset)."""
    import subprocess
    import tempfile

    out_path = tmp_dir / (path.stem + "_frame.jpg")
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-ss", "00:00:01", "-i", str(path),
                "-frames:v", "1", "-q:v", "2", str(out_path),
            ],
            capture_output=True, text=True, timeout=45,
        )
        if proc.returncode == 0 and out_path.exists():
            return out_path
    except Exception as exc:  # noqa: BLE001
        logger.debug("frame extraction failed for %s: %s", path, exc)
    return None


def expand_targets(args) -> list[Path]:
    """Resolve positional paths / --dir / --name into a list of asset files."""
    targets: list[Path] = []
    roots: list[Path] = []
    if args.dir:
        roots.append(Path(args.dir))
    if args.name:
        # Search common output dirs for a filename substring.
        for sub in ("output/images", "output/video_clips",
                    "output/thumbnails", "output/ai_images", "output/ai_scenes"):
            roots.append(PROJECT_ROOT / sub)

    for raw in args.paths:
        p = Path(raw)
        if p.exists():
            if p.is_dir():
                roots.append(p)
            else:
                targets.append(p)
        else:
            # treat as glob pattern
            matches = sorted(Path().glob(raw))
            targets.extend(m for m in matches if m.is_file())

    for root in roots:
        if not root.exists():
            continue
        for entry in sorted(root.rglob("*")):
            if not entry.is_file():
                continue
            if args.name and args.name.lower() not in entry.name.lower():
                continue
            targets.append(entry)

    # De-dup preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for t in targets:
        key = str(t.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detector read-only de logos/overlays en las 4 esquinas.",
    )
    parser.add_argument("paths", nargs="*",
                        help="Archivos, directorios o patrones glob.")
    parser.add_argument("--dir", help="Directorio a recorrer recursivamente.")
    parser.add_argument("--name", help="Buscar assets por nombre (substring).")
    parser.add_argument("--ratio", type=float, default=1.8,
                        help="Ratio borde-esquina/mediana para sospechar (def. 1.8).")
    parser.add_argument("--edge-floor", type=float, default=15.0,
                        help="Densidad mínima de borde para sospechar (def. 15).")
    parser.add_argument("--std-floor", type=float, default=8.0,
                        help="Desviación mínima en la esquina para sospechar (def. 8).")
    parser.add_argument("--corner-frac", type=float, default=0.20,
                        help="Fracción de ancho/alto de cada esquina (def. 0.20).")
    parser.add_argument("--no-video", action="store_true",
                        help="Ignorar archivos de vídeo.")
    parser.add_argument("--json", action="store_true",
                        help="Salida JSON en stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        logger.error("PIL no está disponible — instala Pillow para usar este script.")
        return 1

    targets = expand_targets(args)
    if not targets:
        logger.error("No se encontraron assets para analizar.")
        return 1

    has_numpy = _numpy_available()
    if not has_numpy:
        logger.warning("numpy no disponible — usando detector de bordes PIL (menos preciso).")

    results: list[dict] = []
    import tempfile

    with tempfile.TemporaryDirectory(prefix="asset_logos_") as tmp:
        tmp_dir = Path(tmp)
        for target in targets:
            ext = target.suffix.lower()
            if ext in IMAGE_EXTS:
                results.append(analyze_image(
                    target, args.corner_frac, args.ratio,
                    args.edge_floor, args.std_floor,
                ))
            elif ext in VIDEO_EXTS and not args.no_video:
                frame = extract_video_frame(target, tmp_dir)
                if frame is None:
                    results.append({
                        "path": str(target), "ok": False,
                        "error": "no se pudo extraer frame (ffmpeg)",
                    })
                else:
                    rep = analyze_image(
                        frame, args.corner_frac, args.ratio,
                        args.edge_floor, args.std_floor,
                    )
                    rep["path"] = str(target)
                    rep["analyzed_frame"] = str(frame)
                    results.append(rep)
            else:
                logger.debug("ignorando (extensión no soportada): %s", target)

    flagged = [r for r in results if r.get("suspicious_corners")]

    if args.json:
        print(json.dumps({
            "numpy": has_numpy,
            "analysed": len(results),
            "flagged": len(flagged),
            "results": results,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"Analizados: {len(results)}  |  con esquinas sospechosas: {len(flagged)}")
        for r in results:
            if not r.get("ok"):
                print(f"  [skip] {r['path']}: {r.get('error', 'error')}")
                continue
            corners = r["corners"]
            parts = " ".join(
                f"{name}={corners[name]['edge_mean']}e/"
                f"{corners[name]['std']}s"
                f"{'*' if corners[name]['suspicious'] else ''}"
                for name in CORNERS if name in corners
            )
            tag = f" ⚠ {', '.join(r['suspicious_corners'])}" if r["suspicious_corners"] else ""
            print(f"  {r['path']}{tag}\n      {parts}   (edge/std, * = sospechosa)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
