#!/usr/bin/env python3
"""CLI de reanudación gradual post-strike (antiban, ago 2026).

Envuelve api.services.gradual_resume. Uso:

  python3 scripts/gradual_resume.py --status   # muestra el plan y fases actuales
  python3 scripts/gradual_resume.py --apply    # aplica las fases de hoy (+ replan 7d)
  python3 scripts/gradual_resume.py --dry-run  # simula --apply sin escribir nada
  python3 scripts/gradual_resume.py --reset    # (re)construye el plan desde los bloques
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Root del proyecto (funciona igual en worktree que en producción)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="Reanudación gradual post-strike (antiban)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="muestra el plan y fases actuales")
    g.add_argument("--apply", action="store_true", help="aplica las fases de hoy (+ replan 7d)")
    g.add_argument("--dry-run", action="store_true", help="simula --apply sin escribir nada")
    g.add_argument("--reset", action="store_true", help="(re)construye el plan desde los bloques actuales")
    args = ap.parse_args()

    from database.db_extended import ExtendedDatabase
    from api.services.gradual_resume import (
        apply_resume_phases, build_plan, resume_status,
    )

    db = ExtendedDatabase()

    if args.reset:
        plan = build_plan(db, persist=True)
        print(f"Plan reconstruido para {len(plan)} canal(es):")
        _print_status(resume_status(db))
        return 0

    if args.status:
        _print_status(resume_status(db))
        return 0

    if args.dry_run:
        result = apply_resume_phases(db, replan=False, dry_run=True)
    else:
        result = apply_resume_phases(db, replan=True, dry_run=False)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _print_status(rows: list[dict]) -> None:
    print(f"{'Canal':<10} {'Fuente':<12} {'Inicio (UTC)':<22} {'Fase hoy':<8} Frecuencia actual")
    print("-" * 95)
    for r in rows:
        freq = r["freq"]
        alt = freq.get("alternate_pattern")
        print(
            f"{r['slug']:<10} {r['source']:<12} {r['start_iso'][:19]:<22} "
            f"{r['phase_today']:<8} "
            f"vpd={freq.get('videos_per_day')} alt={alt} "
            f"shorts={freq.get('shorts_native_per_day')} clips={freq.get('shorts_clips_per_long')}"
        )


if __name__ == "__main__":
    sys.exit(main())
