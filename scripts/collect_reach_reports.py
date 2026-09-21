#!/usr/bin/env python3
"""Recolecta los reach reports del YouTube Reporting API (embudo de alcance).

Rellena ``video_reach_daily`` con impresiones de miniatura, CTR, retención,
watch-time y subs por vídeo y día. Es la ÚNICA fuente de impresiones orgánicas:
la Analytics API no las expone (ver ``pipeline/youtube_reach.py``).

Bulk y con **cuota propia** del Reporting API (no consume la Data API). Se
ejecuta a mano (respeta el invariante ``STATS_AUTO_COLLECT=False``).

Uso:
    python3 scripts/collect_reach_reports.py                 # todos los canales activos
    python3 scripts/collect_reach_reports.py --canal canal2
    python3 scripts/collect_reach_reports.py --max-reports 30
    python3 scripts/collect_reach_reports.py --dry-run       # solo lista jobs/tipos
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("collect_reach_reports")


def _resolve_channels(db, slugs: list[str]) -> list[dict]:
    channels = db.get_channels(active_only=True) or []
    if slugs:
        wanted = {s.lower() for s in slugs}
        return [c for c in channels if (c.get("slug") or "").lower() in wanted]
    return [c for c in channels if (c.get("slug") or "") != "test"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--canal", action="append", default=None,
        help="Slug del canal (repetible). Por defecto, todos los activos.",
    )
    parser.add_argument(
        "--max-reports", type=int, default=10,
        help="Máximo de reportes diarios a descargar por job (default 10).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Solo autentica y lista los jobs/tipos, sin descargar ni persistir.",
    )
    args = parser.parse_args(argv)

    from database.db_extended import ExtendedDatabase
    from pipeline.youtube_reach import ReachReportClient, RELEVANT_REPORT_TYPES

    db = ExtendedDatabase()
    channels = _resolve_channels(db, args.canal or [])
    if not channels:
        logger.error("No hay canales que procesar")
        return 1

    totals = {"channels": 0, "reports": 0, "reach_rows": 0, "basic_rows": 0, "errors": 0}
    for ch in channels:
        slug = ch.get("slug")
        client = ReachReportClient(slug)
        if not client.authenticate():
            logger.warning("[%s] sin autenticación — se omite", slug)
            totals["errors"] += 1
            continue

        if args.dry_run:
            jobs = client.ensure_jobs()
            logger.info(
                "[%s] dry-run jobs=%s tipos_disponibles=%s",
                slug, list(jobs.keys()),
                [rt for rt in RELEVANT_REPORT_TYPES if client.report_type_available(rt)],
            )
            continue

        summary = client.sync(db, max_reports_per_job=args.max_reports)
        logger.info("[%s] resumen: %s", slug, summary)
        totals["channels"] += 1
        totals["reports"] += summary.get("reports_downloaded", 0)
        totals["reach_rows"] += summary.get("reach_rows", 0)
        totals["basic_rows"] += summary.get("basic_rows", 0)
        totals["errors"] += summary.get("errors", 0)

    if not args.dry_run:
        logger.info("TOTAL: %s", totals)
    return 0 if totals["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
