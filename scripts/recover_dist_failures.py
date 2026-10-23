#!/usr/bin/env python3
"""Recupera videos en error por fallos distribuidos de render/concat.

Reencola videos cuyo error coincide con patrones transitorios distribuidos
(dist_repair_exhausted, local_fallback_deferred, no compatible node, timeout)
y dispara replan de cobertura.

Uso:
  python3 scripts/recover_dist_failures.py --dry-run    # solo muestra
  python3 scripts/recover_dist_failures.py              # reencola + replan
  python3 scripts/recover_dist_failures.py --video-id 2741  # solo uno

No consume cuota YouTube. Requiere DB accesible.
"""
import argparse, json, re, sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DATABASE_PATH

TRANSIENT_PATTERNS = [
    "dist_repair_exhausted", "local_fallback_deferred",
    "no compatible node", "sin nodo compatible",
    "timeout", "worker exited", "worker_died",
    "ensamblaje local devolvio vacio",
    # 'Fallo el ensamblaje' con recursos sanos = fallo dist con causa
    # generica perdida (pre-fix Fase 2). Recuperable con requeue.
    "fallo el ensamblaje",
]

def find_affected(db_path, video_id=None):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    if video_id:
        rows = conn.execute(
            "SELECT id, canal, channel_id, status, progress_phase, error_message "
            "FROM videos WHERE id=? AND status='error'",
            (video_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, canal, channel_id, status, progress_phase, error_message "
            "FROM videos WHERE status='error' AND created_at > datetime('now', '-7 days') "
            "ORDER BY id DESC LIMIT 50"
        ).fetchall()
    conn.close()
    affected = []
    for r in rows:
        err = (r["error_message"] or "").lower()
        if any(p in err for p in TRANSIENT_PATTERNS):
            affected.append(dict(r))
    return affected

def requeue_video(db_path, video_id):
    """Requeue a failed video as a transient retry."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Find latest failed job
    job = conn.execute(
        "SELECT id FROM generation_jobs WHERE video_id=? AND status='failed' "
        "ORDER BY id DESC LIMIT 1", (video_id,)
    ).fetchone()
    if job:
        conn.execute(
            "UPDATE generation_jobs SET status='queued', finished_at=NULL, "
            "started_at=NULL, error_msg=COALESCE(error_msg,'') || ' [recover_dist]' "
            "WHERE id=?", (job["id"],)
        )
    conn.execute(
        "UPDATE videos SET status='error', progress_phase='queued_retry' WHERE id=?",
        (video_id,)
    )
    conn.commit()
    job_id = job["id"] if job else None
    conn.close()
    return job_id

def trigger_replan():
    """Trigger authoritative replan to fix coverage gaps."""
    try:
        from api.services.planning_service import full_replan
        result = full_replan()
        return result
    except Exception as exc:
        return {"error": str(exc)}

def main():
    parser = argparse.ArgumentParser(description="Recover distributed render/concat failures")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--video-id", type=int, default=None)
    parser.add_argument("--no-replan", action="store_true")
    args = parser.parse_args()

    affected = find_affected(DATABASE_PATH, args.video_id)
    if not affected:
        print("No hay videos afectados por fallos distribuidos.")
        return

    print(f"\n{'='*60}")
    print(f"Videos afectados: {len(affected)}")
    print(f"{'='*60}")
    for v in affected:
        err_short = (v["error_message"] or "")[:120]
        print(f"  #{v['id']} [{v['canal']}] {v['progress_phase']} | {err_short}")

    if args.dry_run:
        print("\n[dry-run] No se modifica nada.")
        return

    print(f"\nReencolando {len(affected)} video(s)...")
    for v in affected:
        job_id = requeue_video(DATABASE_PATH, v["id"])
        print(f"  #{v['id']} -> queued_retry (job={job_id})")

    if not args.no_replan:
        print("\nDisparando replan de cobertura...")
        result = trigger_replan()
        print(f"  Resultado: {json.dumps(result, default=str, indent=2)[:500]}")

    print("\nRecuperacion completada.")

if __name__ == "__main__":
    main()
