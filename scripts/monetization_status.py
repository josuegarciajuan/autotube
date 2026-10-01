#!/usr/bin/env python3
"""Estado de monetización (F2).

Muestra las horas de largos públicos (aproximación Analytics) y la cifra YPP
confirmada por el operador desde YouTube Studio. Permite registrar esa cifra
confirmada con fecha y procedencia.

Uso:
    python3 scripts/monetization_status.py
    python3 scripts/monetization_status.py --json
    python3 scripts/monetization_status.py --set-ypp-hours 1234.5 --note "Studio 2026-10-01"
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="salida JSON")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--set-ypp-hours", type=float, default=None,
                        help="registra la cifra confirmada en Studio")
    parser.add_argument("--note", default="", help="nota/contexto de la cifra YPP")
    args = parser.parse_args(argv)

    from database.db_extended import ExtendedDatabase
    from api.services.monetization import monetization_status, set_ypp_confirmed

    db = ExtendedDatabase()

    if args.set_ypp_hours is not None:
        set_ypp_confirmed(db, args.set_ypp_hours, source="studio", note=args.note)

    status = monetization_status(db, days=args.days)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    print("Horas de largos públicos (aprox. Analytics):")
    for slug, hours in (status["longform_public_hours_analytics"]["by_channel_hours"] or {}).items():
        print(f"  {slug:<8} {hours:>10.1f} h")
    print(f"  {'TOTAL':<8} {status['longform_public_hours_analytics']['total_hours']:>10.1f} h")
    ypp = status["ypp_confirmed"]
    if ypp.get("hours") is not None:
        print(f"\nYPP confirmado (Studio): {ypp['hours']} h "
              f"({ypp.get('progress_pct')}% de {ypp['target_hours']:.0f} h) "
              f"· {ypp.get('confirmed_at')}")
    else:
        print("\nYPP confirmado: (sin registrar) — usa --set-ypp-hours N")
    print(f"\n{ypp and status['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
