#!/usr/bin/env python3
"""Limpieza de residuos del pipeline distribuido (SuperServer) y de jobs.

Tres clases de residuo que se acumulan sin afectar a la generación en curso:

1. Directorios temporales de staging del motor distribuido:
   `/tmp/ss-dist-*` (motor SuperServer) y `/tmp/autotube_concat_dist_*`
   (concat de batches de autotube). Se borran solo si superan una antigüedad
   (`--min-age-hours`, def. 24 h) para no tocar ninguno en uso.

2. Artefactos de ejecución en el control-plane
   (`<taildeck>/distributed/artifacts/<exec>/...`): productos intermedios
   (escenas JPEG, MP4 por batch) ya consumidos por el ensamblado final. Se
   borran los execs con antigüedad > `--artifacts-min-age-hours` (def. 24 h).
   Si el nodo es el control-plane, esto evita el crecimiento sin límite.

3. `returns/` (`<taildeck>/returns/<id>/...`): resultados devueltos por
   trabajos delegados. Se borran con antigüedad > `--returns-min-age-days`
   (def. 3 días).

Además, jobs `generation_jobs` en estado `deferred` cuyo worker ya no existe y
son antiguos (`--min-age-days`, def. 7 días) se marcan `cancelled`.

SEGURO POR DEFECTO: sin `--apply` solo lista lo que haría (dry-run).

Uso:
  python3 scripts/cleanup_dist_orphans.py                       # dry-run
  python3 scripts/cleanup_dist_orphans.py --apply
  python3 scripts/cleanup_dist_orphans.py --apply --min-age-hours 48
  python3 scripts/cleanup_dist_orphans.py --apply --skip-jobs --skip-taildeck
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

# Staging temporal del motor distribuido (control-plane y workers locales).
TMP_STAGING_GLOBS = (
    "/tmp/ss-dist-*",
    "/tmp/autotube_concat_dist_*",
)
# Raíces del control-plane (SuperServer) donde se acumulan residuos.
DEFAULT_TAILDECK_DIR = "/var/lib/taildeck"
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


def _dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
    return total


def _fmt(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024.0:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024.0
    return f"{nbytes:.1f} PB"


def find_stale_tmp_dirs(min_age_hours: float) -> list[str]:
    cutoff = time.time() - min_age_hours * 3600.0
    stale = []
    seen = set()
    for pattern in TMP_STAGING_GLOBS:
        for path in glob.glob(pattern):
            if path in seen or not os.path.isdir(path):
                continue
            seen.add(path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                stale.append(path)
    return sorted(stale)


def find_stale_taildeck_dirs(root: str, subdir: str,
                             min_age_hours: float) -> list[str]:
    base = os.path.join(root, subdir)
    if not os.path.isdir(base):
        return []
    cutoff = time.time() - min_age_hours * 3600.0
    stale = []
    for name in os.listdir(base):
        path = os.path.join(base, name)
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
    try:
        conn = sqlite3.connect(str(DATABASE_PATH), timeout=15)
    except sqlite3.Error as exc:  # DB inexistente/ilegible: no es fatal
        print(f"  [aviso] no se pudo abrir la DB ({exc}); se omite la purga de jobs")
        return []
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
    except sqlite3.Error as exc:  # tabla ausente/DB vacía: no es fatal
        print(f"  [aviso] consulta de jobs omitida ({exc})")
        return []
    finally:
        conn.close()
    # Solo los que NO tienen worker vivo (diferidos de pacing zombies).
    return [r for r in rows if not _pid_alive(r[4])]


def _purge_dirs(dirs: list[str], apply: bool, label: str) -> tuple[int, int]:
    total = 0
    removed = 0
    print(f"\n[{label}] {len(dirs)} huérfanos")
    for d in dirs:
        size = _dir_size_bytes(d)
        total += size
        print(f"  {'rm ' if apply else '~  '}{d}  ({_fmt(size)})")
        if apply:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed, total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="ejecuta la limpieza (por defecto solo dry-run)")
    ap.add_argument("--min-age-hours", type=float, default=24.0,
                    help="antigüedad mínima de staging en /tmp (def. 24 h)")
    ap.add_argument("--min-age-days", type=float, default=7.0,
                    help="antigüedad mínima de jobs deferred (def. 7 días)")
    ap.add_argument("--taildeck-dir", default=DEFAULT_TAILDECK_DIR,
                    help="raíz del control-plane (def. /var/lib/taildeck)")
    ap.add_argument("--artifacts-min-age-hours", type=float, default=24.0,
                    help="antigüedad mínima de distributed/artifacts (def. 24 h)")
    ap.add_argument("--returns-min-age-days", type=float, default=3.0,
                    help="antigüedad mínima de returns (def. 3 días)")
    ap.add_argument("--skip-tmp", action="store_true", help="no tocar staging en /tmp")
    ap.add_argument("--skip-taildeck", action="store_true",
                    help="no tocar artifacts/returns del control-plane")
    ap.add_argument("--skip-jobs", action="store_true", help="no tocar jobs deferred")
    # Compatibilidad con el nombre antiguo.
    ap.add_argument("--skip-dirs", action="store_true",
                    help="(obsoleto) alias de --skip-tmp")
    args = ap.parse_args()

    print(f"DB: {DATABASE_PATH}")
    print(f"Modo: {'APPLY' if args.apply else 'DRY-RUN'}")

    removed_dirs = 0
    freed = 0

    if not (args.skip_tmp or args.skip_dirs):
        stale_tmp = find_stale_tmp_dirs(args.min_age_hours)
        r, b = _purge_dirs(stale_tmp,
                           args.apply,
                           f"/tmp staging */ > {args.min_age_hours:.0f} h")
        removed_dirs += r
        freed += b

    if not args.skip_taildeck:
        stale_art = find_stale_taildeck_dirs(
            args.taildeck_dir, "distributed/artifacts", args.artifacts_min_age_hours)
        r, b = _purge_dirs(
            stale_art, args.apply,
            f"distributed/artifacts > {args.artifacts_min_age_hours:.0f} h")
        removed_dirs += r
        freed += b

        stale_ret = find_stale_taildeck_dirs(
            args.taildeck_dir, "returns", args.returns_min_age_days * 24.0)
        r, b = _purge_dirs(
            stale_ret, args.apply,
            f"returns > {args.returns_min_age_days:.0f} d")
        removed_dirs += r
        freed += b

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

    print(f"\nResumen: dirs_borrados={removed_dirs} liberado~{_fmt(freed)} "
          f"jobs_cancelados={cancelled_jobs} (dry-run={not args.apply})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
