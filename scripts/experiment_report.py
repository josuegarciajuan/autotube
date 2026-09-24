#!/usr/bin/env python3
"""Informe del experimento de recuperación de alcance (Fase 0).

Comando oficial para responder "¿cómo va el experimento?". Solo lectura, 0 cuota.

Uso:
    python3 scripts/experiment_report.py                 # informe legible
    python3 scripts/experiment_report.py --json          # salida JSON
    python3 scripts/experiment_report.py --days 7        # ventana de comparación
    python3 scripts/experiment_report.py --log "texto"   # registra una intervención

Contrato: ``specs/experimento-recuperacion-alcance.md``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _fmt(v, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.1f}{suffix}"
    return f"{v:,}{suffix}"


def _delta_str(v) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:,.2f}"


def _print_report(rep: dict) -> None:
    print("=" * 78)
    print("EXPERIMENTO: recuperación de alcance y ritmo de crecimiento")
    print(f"Inicio: {rep.get('experiment_started_at') or '(sin sellar)'}   "
          f"Generado: {rep.get('generated_at')}")
    w = rep.get("windows", {})
    print(f"Ventana 'ahora': {w.get('now', {}).get('from')} ({w.get('now', {}).get('days')} d)  |  "
          f"'antes': {w.get('pre', {}).get('from')}..{w.get('pre', {}).get('to')}")
    print("=" * 78)

    print("\nINTERVENCIONES REGISTRADAS")
    ints = rep.get("interventions") or []
    if not ints:
        print("  (ninguna registrada — usa: --log \"texto\")")
    for it in ints:
        print(f"  [{it.get('at', '?')}] {it.get('text', '')}")

    print("\nKPIs LEADING (long-form) — CTR / impresiones por vídeo / retención")
    hdr = f"  {'canal':<8} {'CTR ahora':>10} {'CTR antes':>10} {'ΔCTR':>8} " \
          f"{'Impr/vídeo':>11} {'Impr antes':>11} {'Retención':>10}"
    print(hdr)
    for ch in rep.get("channels", []):
        ld = ch.get("leading", {})
        print(f"  {ch.get('slug', '?'):<8} "
              f"{_fmt(ld.get('longform_ctr_pct_now'), '%'):>10} "
              f"{_fmt(ld.get('longform_ctr_pct_pre'), '%'):>10} "
              f"{_delta_str(ld.get('longform_ctr_pct_delta')):>8} "
              f"{_fmt(ld.get('longform_impr_per_video_now')):>11} "
              f"{_fmt(ld.get('longform_impr_per_video_pre')):>11} "
              f"{_fmt(ld.get('longform_retention_now'), '%'):>10}")

    print("\nKPIs LAGGING — subs netos / alcance Shorts 7d")
    print(f"  {'canal':<8} {'subs ahora':>11} {'subs antes':>11} {'Δsubs':>8} {'reach7d':>9}")
    for ch in rep.get("channels", []):
        lg = ch.get("lagging", {})
        print(f"  {ch.get('slug', '?'):<8} "
              f"{_fmt(lg.get('subs_now')):>11} {_fmt(lg.get('subs_pre')):>11} "
              f"{_delta_str(lg.get('subs_net_delta')):>8} {_fmt(lg.get('shorts_reach7d')):>9}")

    print("\nNOTA:", rep.get("note", ""))
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Salida JSON cruda.")
    parser.add_argument("--days", type=int, default=14, help="Ventana de comparación (default 14).")
    parser.add_argument("--log", default=None, help="Registra una intervención en la bitácora.")
    args = parser.parse_args(argv)

    from database.db_extended import ExtendedDatabase
    from api.services.experiment_report import build_report, log_intervention

    db = ExtendedDatabase()

    if args.log:
        ints = log_intervention(args.log, db=db)
        print(f"Intervención registrada ({len(ints)} en total).")
        return 0

    rep = build_report(db, days_now=args.days)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
