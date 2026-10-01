#!/usr/bin/env python3
"""Reproceso histórico del embudo de alcance (F2) — MANUAL, nunca automático.

Re-descarga los informes del Reporting API (``channel_reach_basic_a1``,
``channel_basic_a3``, ``channel_traffic_source_a3``) ya procesados y los vuelve a
persistir con el parser corregido (agrega segmentos por vídeo/día en lugar de
sobrescribirlos). Es **idempotente** (los upserts son deterministas).

No activa la recolección automática: ``STATS_AUTO_COLLECT`` sigue en False. Este
script lo ejecuta el operador cuando quiere corregir el histórico.

Uso:
    python3 scripts/reprocess_reach_reports.py --channel canal3 --dry-run
    python3 scripts/reprocess_reach_reports.py --all
    python3 scripts/reprocess_reach_reports.py --channel canal3 --days 30
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
logger = logging.getLogger("reprocess_reach")


def _slugs(db, only: str | None) -> list[str]:
    with db._connect() as conn:
        if only:
            rows = conn.execute(
                "SELECT slug FROM channels WHERE slug = ?", (only,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT slug FROM channels WHERE active = 1 AND slug != 'test' "
                "ORDER BY id"
            ).fetchall()
    return [r[0] for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=None, help="slug concreto (p. ej. canal3)")
    parser.add_argument("--all", action="store_true", help="todos los canales activos")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="solo informa qué se reprocesaría (no descarga/escribe)")
    args = parser.parse_args(argv)

    if not args.channel and not args.all:
        parser.error("indica --channel <slug> o --all")

    from database.db_extended import ExtendedDatabase
    from pipeline.youtube_reach import ReachReportClient

    db = ExtendedDatabase()
    slugs = _slugs(db, args.channel)
    if not slugs:
        print("Sin canales que reprocesar.")
        return 1

    results = {}
    for slug in slugs:
        client = ReachReportClient(slug)
        if not client.authenticate():
            results[slug] = {"status": "no_auth"}
            print(f"[{slug}] sin autenticación — omitido")
            continue
        if client.api_disabled:
            results[slug] = {"status": "disabled", "hint": client.disabled_hint}
            print(f"[{slug}] Reporting API deshabilitada: {client.disabled_hint}")
            continue
        if args.dry_run:
            jobs = client.ensure_jobs()
            total = 0
            for _rid, job_id in jobs.items():
                try:
                    total += len(client.list_reports(job_id, limit=200))
                except Exception:  # noqa: BLE001
                    pass
            results[slug] = {"status": "dry_run", "jobs": len(jobs), "reports": total}
            print(f"[{slug}] dry-run: {len(jobs)} job(s), {total} informe(s) a reprocesar")
            continue
        summary = client.sync(db, max_reports_per_job=0, ignore_seen=True)
        results[slug] = summary
        print(f"[{slug}] reprocesado: {json.dumps(summary, ensure_ascii=False)}")

    print("\nResumen:", json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
