#!/usr/bin/env python3
"""Limpieza de huérfanos del pipeline distribuido (SuperServer) y de jobs.

Dos clases de residuo que se acumulan sin afectar a la generación en curso:

1. Directorios temporales `/tmp/ss-dist-*` creados por el motor distribuido.
   Se borran solo si superan una antigüedad (`--min-age-hours`, def. 24 h) para
   no tocar ninguno en uso.
2. Jobs `generation_jobs` en estado `deferred` cuyo worker ya no existe y que
   son antiguos (`--min-age-days`, def. 7 días). Se marcan `cancelled`. Los
   diferidos recientes (pacing) se respetan.

SEGURO POR DEFECTO: sin `--apply` solo lista lo que haría (dry-run).

Uso:
  python3 scripts/cleanup_dist_orphans.py                  # dry-run
  python3 scripts/cleanup_dist_orphans.py --apply
  python3 scripts/cleanup_dist_orphans.py --apply --min-age-hours 48
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DATABASE_PATH  # noqa: E402

SS_DIST_GLOB = "/tmp/ss-dist-*"
DEFERRED_ACTIONS = (
    "generate_native_short",
    "generate_clip_short",
    "generate_standalone_short",
)


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    return os.path.exists(f"/proc/{pid}")


def find_stale_dirs(min_age_hours: float) -> list[str]:
    cutoff = time.time() - min_age_hours * 3600.0
    stale = []
    for path in glob.glob(SS_DIST_GLOB):
        if not os.path.isdir(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            stale.append(path)
    return sorted(stale)


def find_stale_jobs(min_age_days: float) -> list[tuple]:
    cutoff = time.time() - min_age_days * 86400.0
    cutoff_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff))
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=15)
    try:
        rows = conn.execute(
            """SELECT id, channel_id, action, status, worker_pid, created_at
               FROM generation_jobs
               WHERE status = 'deferred'
                 AND action IN ({})
                 AND created_at < ?
               ORDER BY id""".format(",".join("?" * len(DEFERRED_ACTIONS))),
            (*DEFERRED_ACTIONS, cutoff_iso),
        ).fetchall()
    finally:
        conn.close()
    # Solo los que NO tienen worker vivo (diferidos de pacing zombies).
    return [r for r in rows if not _pid_alive(r[4])]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="ejecuta la limpieza (por defecto solo dry-run)")
    ap.add_argument("--min-age-hours", type=float, default=24.0,
                    help="antigüedad mínima de dirs /tmp/ss-dist-* (def. 24 h)")
    ap.add_argument("--min-age-days", type=float, default=7.0,
                    help="antigüedad mínima de jobs deferred (def. 7 días)")
    ap.add_argument("--skip-dirs", action="store_true", help="no tocar /tmp/ss-dist-*")
    ap.add_argument("--skip-jobs", action="store_true", help="no tocar jobs deferred")
    args = ap.parse_args()

    print(f"DB: {DATABASE_PATH}")
    print(f"Modo: {'APPLY' if args.apply else 'DRY-RUN'}")

    removed_dirs = 0
    if not args.skip_dirs:
        stale_dirs = find_stale_dirs(args.min_age_hours)
        print(f"\n[/tmp/ss-dist-*] {len(stale_dirs)} huérfanos > {args.min_age_hours:.0f} h")
        for d in stale_dirs:
            print(f"  {'rm ' if args.apply else '~  '}{d}")
            if args.apply:
                shutil.rmtree(d, ignore_errors=True)
                removed_dirs += 1

    cancelled_jobs = 0
    if not args.skip_jobs:
        stale_jobs = find_stale_jobs(args.min_age_days)
        print(f"\n[generation_jobs deferred] {len(stale_jobs)} zombies > "
              f"{args.min_age_days:.0f} días (sin worker vivo)")
        for jid, ch, action, status, pid, created in stale_jobs:
            print(f"  {'cancel ' if args.apply else '~      '}"
                  f"job={jid} ch={ch} {action} pid={pid} creado={created}")
        if args.apply and stale_jobs:
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=15)
            try:
                conn.executemany(
                    """UPDATE generation_jobs
                       SET status='cancelled',
                           error_msg=COALESCE(error_msg,'') || ' [cleanup dist-orphans: deferred zombie]',
                           finished_at=CURRENT_TIMESTAMP
                       WHERE id = ? AND status = 'deferred'""",
                    [(j[0],) for j in stale_jobs],
                )
                conn.commit()
                cancelled_jobs = len(stale_jobs)
            finally:
                conn.close()

    print(f"\nResumen: dirs_borrados={removed_dirs} jobs_cancelados={cancelled_jobs} "
          f"(dry-run={not args.apply})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
