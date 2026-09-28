#!/usr/bin/env python3
"""Batch media reclamation — retención 0 días + barrido retroactivo.

Dos modos, combinables:

1. **Purga de entidades subidas** (``--entity-videos`` / ``--entity-shorts``):
   borra el material pesado de cada vídeo/short confirmado como subido
   (mp4, audio/CTA, escenas + .pollo.json, assets de short_asset_history).
   Preserva thumbnails y el SRT/timestamps principal.

2. **Barrido de huérfanos** (``--orphans``): borra archivos que no referencia
   ninguna entidad, no están bloqueados por un job activo y superan
   ``--min-age-hours``.

Seguridad:
  * nunca borra material de vídeos/shorts NO subidos,
  * nunca borra assets compartidos que otra entidad pendiente referencia,
  * nunca borra archivos en ``media_file_locks`` ni de vídeos en ``error``
    recientes (reensamblado),
  * nunca borra thumbnails ni SRT/timestamps principal.

Uso:
  python3 scripts/cleanup_residuals_batch.py                  # dry-run de todo
  python3 scripts/cleanup_residuals_batch.py --orphans        # dry-run huérfanos
  python3 scripts/cleanup_residuals_batch.py --entity-videos  # dry-run long-forms
  python3 scripts/cleanup_residuals_batch.py --execute --orphans --category audio
  python3 scripts/cleanup_residuals_batch.py --execute --json-out /tmp/manifest.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db_extended import ExtendedDatabase  # noqa: E402
from pipeline import media_retention as mr  # noqa: E402


def format_size(bytes_val: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} TB"


def _uploaded_videos(db, canal=None):
    q = ("SELECT v.id FROM videos v LEFT JOIN channels c ON v.channel_id=c.id "
         "WHERE v.yt_video_id IS NOT NULL AND v.yt_video_id != ''")
    params = ()
    if canal:
        q += " AND c.slug = ?"
        params = (canal,)
    return [r["id"] for r in db._connect().execute(q, params).fetchall()]


def _uploaded_shorts(db, canal=None):
    q = ("SELECT s.id FROM shorts s LEFT JOIN channels c ON s.channel_id=c.id "
         "WHERE s.youtube_id IS NOT NULL AND s.youtube_id != ''")
    params = ()
    if canal:
        q += " AND c.slug = ?"
        params = (canal,)
    return [r["id"] for r in db._connect().execute(q, params).fetchall()]


def _purge_entities(db, kind, ids, dry_run, refs, locked):
    report = {"kind": kind, "count": len(ids), "freed_bytes": 0,
              "files": 0, "errors": 0, "skipped_protected": 0}
    paths = set()
    for entity_id in ids:
        r = mr.purge_entity_media(
            db, kind, entity_id, reason="retro_batch",
            dry_run=dry_run, refs=refs, locked=locked,
        )
        report["freed_bytes"] += r.get("freed_bytes", 0)
        file_list = (r.get("deleted") or []) if not dry_run else (r.get("would_delete") or [])
        report["files"] += len(file_list)
        paths.update(file_list)
        report["errors"] += len(r.get("errors", []))
        report["skipped_protected"] += len(r.get("skipped_protected", []))
    return report, paths


def main() -> int:
    ap = argparse.ArgumentParser(description="Media reclamation (retención 0 días)")
    ap.add_argument("--execute", action="store_true",
                    help="Borrar de verdad (por defecto: dry-run)")
    ap.add_argument("--entity-videos", action="store_true",
                    help="Purgar material de vídeos subidos")
    ap.add_argument("--entity-shorts", action="store_true",
                    help="Purgar material de shorts subidos")
    ap.add_argument("--orphans", action="store_true",
                    help="Barrer archivos huérfanos")
    ap.add_argument("--canal", type=str, default=None,
                    help="Limitar a un canal (slug)")
    ap.add_argument("--category", action="append", default=None,
                    help="Categoría de huérfanos (repetible). Def: todas")
    ap.add_argument("--min-age-hours", type=float, default=24.0,
                    help="Guard de antigüedad para huérfanos (def: 24h)")
    ap.add_argument("--json-out", type=str, default=None,
                    help="Escribir manifiesto JSON")
    args = ap.parse_args()

    # Si no se elige modo, hacer todo.
    if not (args.entity_videos or args.entity_shorts or args.orphans):
        args.entity_videos = args.entity_shorts = args.orphans = True

    dry_run = not args.execute
    db = ExtendedDatabase()

    print("=" * 64)
    print(f"  MEDIA RECLAMATION — {'DRY RUN (sin borrar)' if dry_run else 'EXECUTE'}")
    print("=" * 64)

    refs = mr.build_reference_index(db)
    locked = mr._locked_basenames(db)
    print(f"Referencias: {len(refs['all_refs'])} | "
          f"protegidas (entidades pendientes): {len(refs['protected_refs'])} | "
          f"bloqueadas: {len(locked)}")
    print()

    manifest = {"dry_run": dry_run, "refs": {
        "all": len(refs["all_refs"]),
        "protected": len(refs["protected_refs"]),
        "locked": len(locked),
    }}

    total_freed = 0
    entity_paths: set = set()
    sweep_paths: dict = {}

    if args.entity_videos:
        ids = _uploaded_videos(db, args.canal)
        print(f"── Vídeos subidos a purgar: {len(ids)}")
        vrep, vpaths = _purge_entities(db, "video", ids, dry_run, refs, locked)
        entity_paths |= vpaths
        total_freed += vrep["freed_bytes"]
        print(f"   {format_size(vrep['freed_bytes'])} en {vrep['files']} archivos "
              f"(protegidos: {vrep['skipped_protected']}, errores: {vrep['errors']})")
        manifest["videos"] = vrep

    if args.entity_shorts:
        ids = _uploaded_shorts(db, args.canal)
        print(f"── Shorts subidos a purgar: {len(ids)}")
        srep, spaths = _purge_entities(db, "short", ids, dry_run, refs, locked)
        entity_paths |= spaths
        total_freed += srep["freed_bytes"]
        print(f"   {format_size(srep['freed_bytes'])} en {srep['files']} archivos "
              f"(protegidos: {srep['skipped_protected']}, errores: {srep['errors']})")
        manifest["shorts"] = srep

    if args.orphans:
        print(f"── Huérfanos (min-age {args.min_age_hours}h, categorías: "
              f"{args.category or 'todas'})")
        plan = mr.classify_reclaim(db, args.category, args.min_age_hours)
        for cat, info in plan["categories"].items():
            print(f"   {cat:24s} total={format_size(info['bytes']):>10s} "
                  f"reclaim={format_size(info['reclaim_bytes']):>10s} "
                  f"({info['reclaim_files']}/{info['files']} files)")
        manifest["orphans_classify"] = plan
        if dry_run:
            sweep_paths = mr.plan_reclaim_paths(db, args.category, args.min_age_hours)
            manifest["orphans_planned"] = {
                "files": len(sweep_paths),
                "bytes": sum(sweep_paths.values()),
            }
        else:
            orep = mr.purge_orphans(db, args.category, args.min_age_hours, dry_run=False)
            total_freed += orep["freed_bytes"]
            manifest["orphans"] = orep
            print(f"   Liberado: {format_size(orep['freed_bytes'])}")

    if dry_run:
        # Manifiesto honesto: unión de material de entidades + barrido, sin
        # doble conteo. El total único es lo que se liberaría ejecutando ambos.
        union: dict[str, int] = dict(sweep_paths)
        for p in entity_paths:
            union.setdefault(p, 0)
        # sizes for entity-only paths
        for p in entity_paths:
            if union.get(p, 0) == 0:
                try:
                    union[p] = Path(p).stat().st_size
                except OSError:
                    union[p] = 0
        unique_bytes = sum(union.values())
        manifest["unique_reclaim"] = {"files": len(union), "bytes": unique_bytes}
        total_freed = unique_bytes

    print()
    print("=" * 64)
    print(f"  Total {'a liberar (único)' if dry_run else 'liberado'}: {format_size(total_freed)}")
    print("=" * 64)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        print(f"Manifiesto: {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
