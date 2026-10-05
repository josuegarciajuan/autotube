#!/usr/bin/env python3
"""Purga de logs rotados y backups de BD antiguos (retención de disco).

Dos mecanismos complementarios:

1. **Por antigüedad** (``--days``, def. 14): borra ``logs/**`` con mtime >
   ``--days``, backups de BD en la raíz (``autotube.db.bak*``, ``*.db.bak*``,
   ``autotube.db.corrupt.*``) y ``data/*.bak*`` antiguos.
2. **Por tamaño** (``--max-total-mb``, def. 1000): tras la purga por antigüedad,
   borra los ``.log`` / ``.log.*`` / ``.gz`` más antiguos de ``logs/`` (incluido
   ``logs/obs/`` y su subárbol) hasta bajar del tope. Esto acota el disco aunque
   un día sea anormalmente verboso (p. ej. un ``api.log`` de cientos de MB).

Garantías:
  * **Nunca** borra ficheros trackeados por git (``_tracked_files``).
  * **Nunca** borra el archivo activo del día salvo que, tras vaciar todo lo
    demás, el total siga por encima del tope (último recurso documentado: la
    seguridad de disco manda). El archivo activo se detecta por mtime >= medianoche.
  * Dry-run por defecto. Requiere ``--execute`` para borrar.

Compatibilidad: la llamada actual del timer
``--execute --days 14`` sigue funcionando (``--max-total-mb`` es opcional).
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Log-like extensions the size cap is allowed to delete.
_LOG_SUFFIXES = (".log", ".gz")


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


def _is_log_like(path: Path) -> bool:
    name = path.name
    if name.endswith(_LOG_SUFFIXES):
        return True
    # Rotated names: api.log.1, obs.log.2026-10-05, foo.log.2026-10-05-213000
    return ".log." in name


def _iter_log_files():
    """All log-like files under ``logs/`` (recursive, incl. ``logs/obs/**``)."""
    logs = ROOT / "logs"
    if not logs.is_dir():
        return []
    out = []
    for p in logs.rglob("*"):
        try:
            if p.is_file() and _is_log_like(p):
                out.append(p)
        except OSError:
            continue
    return out


def _today_start() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                        0, 0, 0, 0, 0, -1))


def _plan_size_purge(max_total_mb: float):
    """Return (to_delete, kept_total_after, total_before) for the size cap.

    Order: oldest first; non-active files are purged before today's active
    files (which are only removed if strictly necessary to reach the cap).
    """
    cap_bytes = max_total_mb * 1024 * 1024
    tracked = _tracked_files()
    files = []
    for p in _iter_log_files():
        try:
            rp = str(p.resolve())
        except OSError:
            continue
        if rp in tracked:
            continue
        try:
            files.append((p.stat().st_mtime, p.stat().st_size, p))
        except OSError:
            continue

    total_before = sum(size for _, size, _ in files)
    if total_before <= cap_bytes:
        return [], total_before, total_before

    today_start = _today_start()
    non_active = sorted(
        (item for item in files if item[0] < today_start), key=lambda x: x[0]
    )
    active = sorted(
        (item for item in files if item[0] >= today_start), key=lambda x: x[0]
    )

    to_delete = []
    running = total_before
    for ordered in (non_active, active):
        for _, size, path in ordered:
            if running <= cap_bytes:
                break
            to_delete.append(path)
            running -= size
        if running <= cap_bytes:
            break
    return to_delete, running, total_before


def _run_size_purge(max_total_mb: float, execute: bool) -> int:
    to_delete, remaining, total_before = _plan_size_purge(max_total_mb)
    print("=" * 60)
    print(f"  TAMAÑO — tope {max_total_mb:.0f} MB · "
          f"{format_size(total_before)} actuales, {format_size(remaining)} tras purga")
    print(f"  {'DRY RUN' if not execute else 'EXECUTE'} — {len(to_delete)} archivos")
    print("=" * 60)
    for p in to_delete:
        try:
            rel = p.relative_to(ROOT)
        except ValueError:
            rel = p
        print(f"  {rel}")
    if execute:
        freed = 0
        errors = 0
        for p in to_delete:
            try:
                freed += p.stat().st_size
                p.unlink()
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"  ERROR {p}: {exc}")
        print(f"Liberado por tamaño: {format_size(freed)} ({errors} errores)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Purga logs rotados + backups antiguos")
    ap.add_argument("--execute", action="store_true", help="Borrar (def: dry-run)")
    ap.add_argument("--days", type=float, default=14.0, help="Antigüedad mínima (días)")
    ap.add_argument(
        "--max-total-mb", type=float, default=1000.0,
        help="Tope total de logs (MB). Tras la purga por antigüedad se borran "
             "los .log/.log.*/.gz más antiguos hasta bajar de este tope "
             "(def. 1000).",
    )
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

    _run_size_purge(args.max_total_mb, args.execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
