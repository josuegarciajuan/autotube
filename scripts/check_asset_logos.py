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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Fase 4b: the corner-overlay analysis lives in pipeline.visual_verifier and is
# shared with the observation-mode verifier (single source of truth).
from pipeline.visual_verifier import (  # noqa: E402
    CORNERS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    _numpy_available,
    analyze_image,
    extract_video_frame,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("check_asset_logos")


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
