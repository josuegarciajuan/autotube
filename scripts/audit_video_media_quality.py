#!/usr/bin/env python3
"""Fase 0 — Auditoría read-only de la calidad/coherencia del media.

Vuelca, por vídeo y por escena, lo que realmente se guardó para poder medir
sin cambiar comportamiento:

  - narración del bloque (``texto``) y ``search_query_en`` planificados;
  - asset final (ruta, tipo video/imagen, proveedor inferido del nombre);
  - dimensiones/resolución reales del asset (PIL para imágenes, ffprobe para
    vídeo), si el archivo sigue en disco;
  - porcentaje de vídeo por tiempo y por escena.

Fuentes (todas opcionales y leídas con introspección defensiva de esquema):
  - ``videos``              → id, canal, script_id, status, duracion_seg
  - ``scripts.bloques_json``→ narración + ``search_query_en`` + media_duracion
  - ``video_scenes``        → mapeo escena → asset (``image_path``)
  - ``video_asset_history`` → assets reales cuando ``video_scenes`` no existe

NO escribe en la base de datos. Solo lee (``mode=ro``).

Usage:
    python3 scripts/audit_video_media_quality.py --canal canal2 --last 5
    python3 scripts/audit_video_media_quality.py --video-id 2526 --json
    python3 scripts/audit_video_media_quality.py --canal canal2 --last 10 \
        --out /tmp/audit_canal2.json --no-probe
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DATABASE_PATH  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("audit_video_media_quality")

# ── Provider inference (from filename prefix) ─────────────────────
# Longest-first so "pixabay_photo" wins over "pixabay".
PROVIDER_PREFIXES = [
    "pixabay_photo", "pixabay_video", "pixabay",
    "pexels_photo", "pexels_video", "pexels",
    "unsplash", "mixkit", "coverr", "youtube_cc",
    "pollinations", "local_sd", "pollo_ai", "pollo", "ai_image",
]

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


# ── Schema introspection helpers (defensive) ─────────────────────

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return column names for *table*; empty set if it cannot be read."""
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _select_cols(conn, table: str, order_by: str | None = None,
                 where: str = "", params: tuple = ()) -> list[dict]:
    """Select all columns of *table* (defensively) with optional ORDER BY."""
    cols = _columns(conn, table)
    if not cols:
        return []
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    try:
        cur = conn.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        logger.warning("query on %s failed: %s", table, exc)
        return []


# ── Inference / probing ─────────────────────────────────────────

def infer_type(path: str) -> str:
    if not path:
        return "unknown"
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return "unknown"


def infer_provider(path: str, fallback_source: str = "") -> str:
    """Infer provider from the asset filename (DB source wins).

    ``image_processor`` renames every downloaded asset to ``processed_<stem>``,
    so known processing prefixes are stripped before matching. AI-generated
    images are named ``scene_<idx>_<hash>`` by ``media_fetcher``.
    """
    if fallback_source:
        return fallback_source
    name = Path(path or "").name.lower()
    for proc_prefix in ("processed_", "upscaled_"):
        while name.startswith(proc_prefix):
            name = name[len(proc_prefix):]
    for prefix in sorted(PROVIDER_PREFIXES, key=len, reverse=True):
        if name.startswith(prefix):
            return prefix
    if name.startswith("scene_"):
        return "ai_image"  # Pollinations / Local SD cache naming
    # Substring fallback (e.g. unknown prefix + "pixabay_photo" in the name).
    for prefix in sorted(PROVIDER_PREFIXES, key=len, reverse=True):
        if prefix in name:
            return prefix
    return "unknown"


def resolve_path(path: str, root: Path) -> Path | None:
    if not path:
        return None
    p = Path(path)
    if p.is_absolute():
        return p
    return root / path


def probe_dimensions(path: Path | None, media_type: str,
                     cache: dict[str, dict]) -> dict[str, Any]:
    """Return {'width','height'} for an asset, best-effort and never raising."""
    if path is None:
        return {"width": None, "height": None}
    key = str(path)
    if key in cache:
        return cache[key]
    result: dict[str, Any] = {"width": None, "height": None}
    try:
        if path.exists():
            if media_type == "image":
                from PIL import Image
                with Image.open(path) as img:
                    w, h = img.size
                result = {"width": int(w), "height": int(h)}
            elif media_type == "video":
                proc = subprocess.run(
                    [
                        "ffprobe", "-v", "error",
                        "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=s=x:p=0", str(path),
                    ],
                    capture_output=True, text=True, timeout=20,
                )
                out = (proc.stdout or "").strip()
                if proc.returncode == 0 and "x" in out:
                    w_str, h_str = out.split("x")[:2]
                    result = {"width": int(w_str), "height": int(h_str)}
    except Exception as exc:  # noqa: BLE001 — probing must never crash the audit
        logger.debug("could not probe %s: %s", path, exc)
    cache[key] = result
    return result


# ── Data loading ────────────────────────────────────────────────

def load_blocks(conn: sqlite3.Connection, script_id: Any) -> list[dict]:
    if not script_id or not _table_exists(conn, "scripts"):
        return []
    cols = _columns(conn, "scripts")
    json_col = next(
        (c for c in ("bloques_json", "escenas_json") if c in cols), None,
    )
    if not json_col:
        return []
    try:
        row = conn.execute(
            f"SELECT {json_col} FROM scripts WHERE id=?", (script_id,),
        ).fetchone()
    except sqlite3.Error:
        return []
    if not row or not row[0]:
        return []
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("bloques") or data.get("scenes") or []
    return data if isinstance(data, list) else []


def _match_block(scene_desc: str, blocks: list[dict]) -> dict | None:
    """Best-effort scene→block match by visual description prefix."""
    if not scene_desc:
        return None
    sd = " ".join(scene_desc.strip().lower().split())
    for block in blocks:
        ed = " ".join((block.get("escena_descripcion") or "").strip().lower().split())
        if not ed:
            continue
        if ed in sd or sd.startswith(ed[:40]) or ed[:40] == sd[:40]:
            return block
    return None


def _duration_for(scene: dict, block: dict | None) -> float | None:
    """Scene duration (s): planned media_duracion wins, else duration_ms."""
    if block and block.get("media_duracion"):
        try:
            d = float(block["media_duracion"])
            if 0 < d < 600:
                return d
        except (TypeError, ValueError):
            pass
    if scene.get("duration_ms"):
        try:
            d = float(scene["duration_ms"]) / 1000.0
            if 0 < d < 3600:
                return d
        except (TypeError, ValueError):
            pass
    return None


def build_scene_rows(
    conn: sqlite3.Connection,
    video: dict,
    blocks: list[dict],
    assets: list[dict],
    root: Path,
    probe: bool,
    min_width: int,
    cache: dict[str, dict],
) -> list[dict]:
    scenes_table = _table_exists(conn, "video_scenes")
    scene_rows: list[dict] = []
    if scenes_table:
        order_col = "scene_order" if "scene_order" in _columns(conn, "video_scenes") else "id"
        scene_rows = _select_cols(
            conn, "video_scenes",
            order_by=order_col,
            where="video_id = ?", params=(video.get("id"),),
        )

    rows: list[dict] = []

    if scene_rows:
        for i, scene in enumerate(scene_rows):
            block = _match_block(scene.get("description") or "", blocks)
            if block is None and i < len(blocks):
                block = blocks[i]
            path = scene.get("image_path") or scene.get("image_url") or ""
            if not path and i < len(assets):
                path = assets[i].get("file_path") or assets[i].get("asset_url") or ""
            rows.append(_make_row(i, block, scene, path, assets, root, probe,
                                  min_width, cache))
        return rows

    # Fallback: blocks are the canonical scene list (no video_scenes mapping).
    for i, block in enumerate(blocks):
        path = ""
        if i < len(assets):
            path = assets[i].get("file_path") or assets[i].get("asset_url") or ""
        rows.append(_make_row(i, block, {}, path, assets, root, probe,
                              min_width, cache))
    return rows


def _make_row(
    idx: int,
    block: dict | None,
    scene: dict,
    path: str,
    assets: list[dict],
    root: Path,
    probe: bool,
    min_width: int,
    cache: dict[str, dict],
) -> dict:
    block = block or {}
    media_type = infer_type(path)
    db_source = ""
    for asset in assets:
        if asset.get("file_path") == path or asset.get("asset_url") == path:
            db_source = asset.get("source", "") or ""
            break
    provider = infer_provider(path, db_source)
    dims = {"width": None, "height": None}
    if probe and path:
        dims = probe_dimensions(resolve_path(path, root), media_type, cache)
    width, height = dims.get("width"), dims.get("height")
    return {
        "order": idx,
        "narration": block.get("texto", "") or scene.get("script_text", "") or "",
        "search_query_en": block.get("search_query_en", "") or "",
        "media_tipo_planned": block.get("media_tipo", "") or "",
        "duration_s": _duration_for(scene, block),
        "asset_path": path,
        "asset_type": media_type,
        "provider": provider,
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}" if width and height else None,
        "low_res_declared": bool(width and width < min_width),
        "quality_flag": "low_res" if (width and width < min_width) else None,
    }


def build_video_report(conn, video: dict, root: Path, probe: bool,
                       min_width: int, cache: dict[str, dict]) -> dict:
    blocks = load_blocks(conn, video.get("script_id"))
    assets: list[dict] = []
    if _table_exists(conn, "video_asset_history"):
        assets = _select_cols(
            conn, "video_asset_history",
            order_by="id", where="video_id = ?", params=(video.get("id"),),
        )
    scenes = build_scene_rows(conn, video, blocks, assets, root, probe,
                              min_width, cache)

    n_scenes = len(scenes)
    n_video = sum(1 for s in scenes if s["asset_type"] == "video")
    durations = [s["duration_s"] for s in scenes if s.get("duration_s")]
    total_dur = sum(durations) if durations else 0.0
    video_dur = sum(
        s["duration_s"] for s in scenes
        if s["asset_type"] == "video" and s.get("duration_s")
    )
    return {
        "id": video.get("id"),
        "canal": video.get("canal"),
        "script_id": video.get("script_id"),
        "status": video.get("status"),
        "duration_s": video.get("duracion_seg"),
        "n_scenes": n_scenes,
        "n_video_scenes": n_video,
        "n_assets": len(assets),
        "video_pct_by_scene": round(100.0 * n_video / n_scenes, 1) if n_scenes else None,
        "video_pct_by_time": (
            round(100.0 * video_dur / total_dur, 1) if total_dur else None
        ),
        "scenes": scenes,
    }


# ── Output ──────────────────────────────────────────────────────

def print_summary(report: dict) -> None:
    print("=" * 78)
    print(f"DB: {report['database']}")
    if report.get("filters"):
        print(f"Filtros: {report['filters']}")
    print("=" * 78)
    for video in report["videos"]:
        print(
            f"\nVídeo {video['id']} [{video['canal']}] status={video['status']} "
            f"scenes={video['n_scenes']} assets={video['n_assets']} "
            f"vídeo(escena)={video['video_pct_by_scene']}% "
            f"vídeo(tiempo)={video['video_pct_by_time']}%"
        )
        for scene in video["scenes"]:
            res = scene["resolution"] or "?"
            narr = (scene["narration"] or "")[:70].replace("\n", " ")
            flag = f" [{scene['quality_flag']}]" if scene.get("quality_flag") else ""
            print(
                f"  #{scene['order']:>3} {scene['asset_type']:>7} "
                f"{scene['provider']:<14} {res:>10}{flag}"
            )
            print(f"        query: {scene['search_query_en']!r}")
            print(f"        narr : {narr!r}")
    if not report["videos"]:
        print("(sin vídeos para los filtros dados)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auditoría read-only de calidad de media por vídeo/escena.",
    )
    parser.add_argument("--canal", help="Slug del canal (ej. canal2).")
    parser.add_argument("--last", type=int, default=5,
                        help="Número de vídeos recientes (default 5).")
    parser.add_argument("--video-id", type=int, default=None,
                        help="Auditar un único vídeo por id.")
    parser.add_argument("--out", help="Escribir el JSON a este archivo.")
    parser.add_argument("--json", action="store_true",
                        help="Imprimir el JSON en stdout además del resumen.")
    parser.add_argument("--no-probe", action="store_true",
                        help="No abrir assets para leer dimensiones (más rápido).")
    parser.add_argument("--min-width", type=int, default=1280,
                        help="Umbral para marcar low_res (default 1280).")
    parser.add_argument("--db", default=DATABASE_PATH,
                        help="Ruta a la SQLite (default: DATABASE_PATH).")
    parser.add_argument("--root", default=str(PROJECT_ROOT),
                        help="Raíz para resolver rutas relativas de assets "
                             "(default: raíz del repo desde el que se ejecuta).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.canal and args.video_id is None:
        logger.error("Debes indicar --canal <slug> o --video-id <id>.")
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("No existe la base de datos: %s", db_path)
        return 2

    # read-only: never write to the live DB
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        logger.error("No puedo abrir la DB en modo read-only: %s", exc)
        return 2

    try:
        videos: list[dict] = []
        if args.video_id is not None:
            videos = _select_cols(
                conn, "videos", where="id = ?", params=(args.video_id,),
            )
        elif args.canal:
            videos = _select_cols(
                conn, "videos",
                order_by="id DESC",
                where="canal = ?", params=(args.canal,),
            )[: max(1, int(args.last))]

        root = Path(args.root)
        cache: dict[str, dict] = {}
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": str(db_path),
            "root": str(root),
            "mode": "read_only",
            "filters": {
                "canal": args.canal,
                "last": args.last if args.video_id is None else None,
                "video_id": args.video_id,
                "min_width": args.min_width,
                "probe": not args.no_probe,
            },
            "videos": [
                build_video_report(conn, v, root, not args.no_probe,
                                   int(args.min_width), cache)
                for v in videos
            ],
        }
    finally:
        conn.close()

    print_summary(report)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        logger.info("JSON escrito en %s", out_path)
    if args.json or not args.out:
        # When no --out is given, always emit the JSON so it can be piped.
        print("\n--- JSON ---")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
