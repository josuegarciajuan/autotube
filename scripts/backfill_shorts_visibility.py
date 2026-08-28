#!/usr/bin/env python3
"""Backfill/reconcile one-shot del estado real de publicación de shorts (0 cuota).

Contexto (ago 2026): los shorts se marcaban status='published' + published_at=now
al subir, aunque fuesen privados con publishAt futuro, y nunca se verificaba que
realmente se publicaran. Este script reconcilia la verdad externa (yt-dlp + RSS)
en las columnas derivadas (yt_visibility / yt_checked_at / published_at) de los
shorts existentes, para que la UI/endpoints vean la realidad.

NOTA: NO se rellena publish_at desde shorts_planned_slots.target_upload_at porque
ese campo es la hora de SUBIDA (slot de dispatch), no la hora de publicación real
(que se calculaba con _safe_publish_at y nunca se persistía). Usarlo como
publish_at dispararía falsos 'stuck' en shorts ya publicados. Los shorts NUEVOS ya
guardan publish_at correctamente en el momento de subir.

Uso:
    python3 scripts/backfill_shorts_visibility.py             # reconcile (0 cuota)
    python3 scripts/backfill_shorts_visibility.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_shorts_visibility")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="solo loguear, no escribir en BD")
    args = ap.parse_args()

    from database.db_extended import ExtendedDatabase
    from api.services.yt_state_reconciler import reconcile_recent_shorts
    db = ExtendedDatabase()

    if args.dry_run:
        logger.info("Dry-run: reconciliación real de shorts (sin escribir)...")
        # El reconciliador escribe; en dry-run simplemente no lo llamamos.
        logger.info("  (dry-run: no se modificó nada)")
        return

    summary = reconcile_recent_shorts(db)
    logger.info("Reconcile shorts: %s", summary)


if __name__ == "__main__":
    main()
