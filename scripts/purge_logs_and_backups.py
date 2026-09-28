#!/usr/bin/env python3
"""Purga de logs rotados y backups de BD antiguos (retención de disco).

Borra:
  * ``logs/*`` con mtime > ``--days`` (def. 14), excepto los logs activos del
    día (nunca toca ``logs/api.log`` / ``logs/vite_dev.log`` en curso: solo se
    borran si superan la antigüedad, no por nombre).
  * backups de BD en la raíz: ``autotube.db.bak*``, ``*.db.bak*``,
    ``autotube.db.corrupt.*`` con antigüedad > ``--days``.
  * ``data/*.bak*`` con antigüedad > ``--days``.

Dry-run por defecto. Uso:
  python3 scripts/purge_logs_and_backups.py
  python3 scripts/purge_logs_and_backups.py --execute --days 14
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def format_size(b: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(b) < 1024.0:
            return f"{b:.1f} {unit}"
        b /= 1024.0
    return f"{b:.1f} TB"


def _tracked_files() -> set:
    """Absolute paths of files tracked by git (must never be deleted).

    Some stale DB backups are versioned in the repo (``*.bak``); deleting them
    would dirty the production tree and is forbidden. The repo pre-commit hook
    also blocks committing their removal, so the only safe behaviour is to skip
    them. Best-effort: if git is unavailable, returns an empty set.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            return {
                str((ROOT / p).resolve())
                for p in out.stdout.split("\0") if p
            }
    except Exception:  # noqa: BLE001
        pass
    return set()


def _collect(min_age_days: float):
    cutoff = time.time() - min_age_days * 86400.0
    tracked = _tracked_files()
    candidates = []
    logs = ROOT / "logs"
    if logs.is_dir():
        for p in logs.rglob("*"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                candidates.append(p)
    patterns = ["autotube.db.bak*", "*.db.bak*", "autotube.db.corrupt.*",
                "*.corrupt.*", "*.bak"]
    for pat in patterns:
        for p in ROOT.glob(pat):
            if p.is_file() and p.stat().st_mtime < cutoff:
                candidates.append(p)
    data = ROOT / "data"
    if data.is_dir():
        for p in data.glob("*.bak*"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                candidates.append(p)
    # dedup preserving order, skipping git-tracked files
    seen = set()
    out = []
    for p in candidates:
        rp = str(p.resolve())
        if rp in seen or rp in tracked:
            continue
        seen.add(rp)
        out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Purga logs rotados + backups antiguos")
    ap.add_argument("--execute", action="store_true", help="Borrar (def: dry-run)")
    ap.add_argument("--days", type=float, default=14.0, help="Antigüedad mínima (días)")
    args = ap.parse_args()

    targets = _collect(args.days)
    total = 0
    for p in targets:
        try:
            total += p.stat().st_size
        except OSError:
            pass

    print("=" * 60)
    print(f"  {'DRY RUN' if not args.execute else 'EXECUTE'} — {len(targets)} archivos, "
          f"{format_size(total)} (>{args.days}d)")
    print("=" * 60)
    for p in targets:
        print(f"  {p.relative_to(ROOT)}")
    if args.execute:
        freed = 0
        errors = 0
        for p in targets:
            try:
                freed += p.stat().st_size
                p.unlink()
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"  ERROR {p}: {exc}")
        print(f"Liberado: {format_size(freed)} ({errors} errores)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
