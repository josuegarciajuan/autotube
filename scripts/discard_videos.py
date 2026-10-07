#!/usr/bin/env python3
"""Descarta (soft) vídeos en producción y resuelve sus alertas activas.

No borra registros: marca ``status='discarded'`` para conservar el histórico,
que es lo mismo que se hizo con los vídeos descartados previos (2522/2523/2526).

Uso:
    # Previsualización (por defecto, no escribe nada)
    python3 scripts/discard_videos.py --ids 2545,2556,2562
    # Aplicar (marca discarded + resuelve alertas del vídeo)
    python3 scripts/discard_videos.py --ids 2545,2556,2562 --apply
    # Aplicar y además mover mp4/segmentos a output/rejected/
    python3 scripts/discard_videos.py --ids 2545 --apply --archive

Idempotente: un vídeo ya descartado se omite. Nunca toca vídeos con un job
activo (queued/running). No toca miniaturas (invariante de preservación).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DISCARD_REASON = (
    "Discarded: render_gate false-positive branding / fallo terminal (oct 2026)"
)


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def _archive_path(src: Path, dest_dir: Path, label: str) -> str | None:
    """Mueve ``src`` a ``dest_dir`` si existe. Devuelve la ruta destino o None."""
    if not src.exists():
        return None
    dest = dest_dir / label
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(src), str(dest))
    return str(dest)


def discard_video(db, vid: int, *, apply: bool, archive_dir: Path | None) -> dict:
    v = db.get_video(vid)
    if not v:
        return {"id": vid, "action": "not_found"}
    if (v.get("status") or "") == "discarded":
        return {"id": vid, "action": "already_discarded"}

    with db._connect() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM generation_jobs "
            "WHERE video_id = ? AND status IN ('queued', 'running')",
            (vid,),
        ).fetchone()["c"]
    if active:
        return {"id": vid, "action": "skipped_active_job"}

    open_alerts = 0
    with db._connect() as conn:
        open_alerts = conn.execute(
            "SELECT COUNT(*) AS c FROM pipeline_alerts "
            "WHERE resolved = 0 AND entity_type = 'video' AND entity_id = ?",
            (vid,),
        ).fetchone()["c"]

    result = {
        "id": vid,
        "action": "would_discard" if not apply else "discarded",
        "status": v.get("status"),
        "alerts_resolved": 0,
        "archived": [],
    }

    if not apply:
        result["alerts_resolved"] = open_alerts
        return result

    db.update_video(
        vid,
        status="discarded",
        progress_phase="discarded",
        error_message=DISCARD_REASON,
    )

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id FROM pipeline_alerts "
            "WHERE resolved = 0 AND entity_type = 'video' AND entity_id = ?",
            (vid,),
        ).fetchall()
        for r in rows:
            conn.execute(
                """UPDATE pipeline_alerts
                   SET resolved = 1, resolved_at = datetime('now'), acknowledged = 1,
                       message = COALESCE(message, '') ||
                         ' [Descartado por el operador]'
                   WHERE id = ?""",
                (r["id"],),
            )
        conn.commit()
    result["alerts_resolved"] = len(rows)

    if archive_dir is not None:
        vp = v.get("video_path")
        if vp:
            src = Path(vp)
            if not src.is_absolute():
                src = Path.cwd() / src
            moved = _archive_path(src, archive_dir, src.name)
            if moved:
                result["archived"].append(moved)
        seg = Path("output/videos/segments") / str(vid)
        moved = _archive_path(seg, archive_dir, f"segments_{vid}")
        if moved:
            result["archived"].append(moved)

    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Soft-discard de vídeos y limpieza de alertas")
    ap.add_argument("--ids", required=True, help="IDs separados por comas (p.ej. 2545,2556)")
    ap.add_argument("--apply", action="store_true", help="Aplicar (sin esto es dry-run)")
    ap.add_argument("--archive", action="store_true",
                    help="Mover mp4/segmentos a output/rejected/ (reversible)")
    ap.add_argument("--db", default=os.environ.get("DATABASE_PATH", "/root/autotube/autotube.db"))
    args = ap.parse_args()

    vids = _parse_ids(args.ids)
    if not vids:
        print("Nada que hacer: --ids vacío.")
        return 2

    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase(args.db)

    archive_dir = None
    if args.apply and args.archive:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_dir = Path("output/rejected") / f"discarded_{ts}"

    print(f"Modo: {'APLICAR' if args.apply else 'DRY-RUN'} · vídeos={vids}")
    counts: dict[str, int] = {}
    for vid in vids:
        res = discard_video(db, vid, apply=args.apply, archive_dir=archive_dir)
        counts[res["action"]] = counts.get(res["action"], 0) + 1
        print(
            f"  #{vid}: {res['action']}"
            + (f" (status previo={res.get('status')})" if res.get("status") else "")
            + (f" · alertas resueltas={res['alerts_resolved']}" if res.get("alerts_resolved") else "")
            + (f" · archivado={len(res['archived'])}" if res.get("archived") else "")
        )

    print("Resumen:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not args.apply:
        print("Dry-run: repite con --apply para ejecutar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
