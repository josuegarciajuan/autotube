#!/usr/bin/env python3
"""Descarta los artefactos de una corrida de prueba distribuida SIN tocar producción.

Garantías:
  - Comprueba que el ``video_id`` NO existe en la DB de producción; si existe,
    aborta sin borrar nada (producción manda).
  - Por defecto MUEVE los artefactos a ``output/rejected/<run_id>/`` (reversible);
    con ``--delete`` los borra.
  - Limpia: ``output/dist_test/<run_id>``, el mp4 del piloto, los segmentos
    ``output/videos/segments/<video_id>`` y el thumbnail ``thumb_<video_id>.jpg``.

Uso:
    python3 scripts/discard_dist_test.py --run-id <id> --video-id 2521 \
        --work-root /ruta/al/worktree-del-piloto \
        [--video-path output/videos/narration_xxx.mp4] [--delete]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from pathlib import Path


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _fmt(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _rm(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _prod_has_video(db_path: str, video_id: int):
    """True/False si se pudo leer; None si la DB no es accesible (fail-safe)."""
    if not db_path or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Descartar artefactos de una corrida de prueba distribuida")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--video-id", type=int, required=True)
    ap.add_argument("--work-root", required=True, help="Raíz del árbol donde corrió el piloto")
    ap.add_argument("--video-path", default=None, help="Ruta del mp4 (relativa a --work-root)")
    ap.add_argument("--segments-id", default=None,
                    help="Id del directorio de segmentos si difiere del video_id "
                         "(el piloto usa job_id cuando no hay video_id)")
    ap.add_argument("--production-db",
                    default=os.environ.get("AUTOTUBE_PRODUCTION_DB", "/root/autotube/autotube.db"))
    ap.add_argument("--delete", action="store_true", help="Borrar en vez de mover a output/rejected/")
    args = ap.parse_args()

    in_prod = _prod_has_video(args.production_db, args.video_id)
    if in_prod is True:
        print(f"ABORTADO: el video id {args.video_id} EXISTE en producción "
              f"({args.production_db}). No se toca nada.")
        return 2
    if in_prod is None:
        print(f"Aviso: no pude leer la DB de producción ({args.production_db}); "
              f"continúo porque el run vive en un worktree de prueba.")

    w = Path(args.work_root).resolve()
    targets: list[Path] = [w / "output" / "dist_test" / args.run_id]
    if args.video_path:
        vp = Path(args.video_path)
        targets.append(vp if vp.is_absolute() else (w / vp))
    targets.append(w / "output" / "videos" / "segments" / str(args.video_id))
    if args.segments_id:
        targets.append(w / "output" / "videos" / "segments" / str(args.segments_id))
    targets += list((w / "output" / "thumbnails").glob(f"*/thumb_{args.video_id}.jpg"))

    rejected = w / "output" / "rejected" / args.run_id
    total = 0
    for t in targets:
        if not t.exists():
            print(f"  (no existe) {t}")
            continue
        size = _size(t)
        total += size
        if args.delete:
            _rm(t)
            print(f"  borrado  {t}  ({_fmt(size)})")
        else:
            dest = rejected / t.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _rm(dest)
            shutil.move(str(t), str(dest))
            print(f"  movido   {t}  ->  {dest}  ({_fmt(size)})")

    action = "Borrado" if args.delete else f"Movido a {rejected}"
    print(f"{action}. Total: {_fmt(total)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
