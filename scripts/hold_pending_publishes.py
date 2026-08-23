#!/usr/bin/env python3
"""
Emergency script: hold (set to Private) pending scheduled publishes via YouTube Studio.

Contexto (ago 2026): cuando la cuota del Data API está agotada, el repack NO puede
reprogramar los publishAt de vídeos ya subidos como privado (set_publish_at = 403).
Si esos publishAt están vencidos o a punto de vencer, YouTube los suelta en ráfaga
(el patrón que alimentó los strikes de spam). Este script usa YouTube Studio
(Playwright, 0 cuota) para poner en 'Privado' — cancelando el publishAt — los
vídeos en riesgo, de modo que NO puedan publicarse hasta que se re-programen
después (repack con cuota libre).

Usage:
    python3 scripts/hold_pending_publishes.py --dry-run
    python3 scripts/hold_pending_publishes.py --canal canal2
    python3 scripts/hold_pending_publishes.py --canal canal2 --ids 2123,2183,2124
    DATABASE_PATH=/root/autotube/autotube.db YT_BROWSER_TOKENS_DIR=/root/autotube/tokens \
        python3 scripts/hold_pending_publishes.py --canal canal2

Reglas de selección (solo publish_mode='scheduled' con yt_video_id):
  - --past-only (default): target_public_at < now + --buffer-hours (vencidos/inminentes)
  - sin --past-only: TODOS los uploaded_private/warming/scheduled pendientes del canal

Idempotente: registra system_state publish_hold_done_{yt_id} tras cada éxito.
"""

import argparse
import logging
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("hold_publishes")


def _db_path() -> Path:
    env = Path(__file__).resolve().parent.parent / "autotube.db"
    import os
    return Path(os.getenv("DATABASE_PATH", str(env)))


def _parse_utc(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def get_pending(db_path: Path, canal: str = None, past_only: bool = True,
                buffer_hours: float = 0.0, ids: list = None) -> list:
    """Vídeos scheduled pendientes en riesgo (vencidos o inminentes)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    query = """
        SELECT v.id, v.canal, v.yt_video_id, v.target_public_at, v.status
        FROM videos v
        WHERE v.publish_mode = 'scheduled'
          AND v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
          AND v.status IN ('uploaded_private', 'warming', 'scheduled')
    """
    params = []
    if canal:
        query += " AND v.canal = ?"
        params.append(canal)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        query += f" AND v.id IN ({placeholders})"
        params.extend(ids)
    query += " ORDER BY v.target_public_at"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=buffer_hours)
    pending = []
    for r in rows:
        d = dict(r)
        if not d.get("yt_video_id"):
            continue
        if past_only:
            tgt = _parse_utc(d.get("target_public_at"))
            if tgt is None or tgt > horizon:
                continue
        d["target_iso"] = tgt.isoformat() if tgt else None
        pending.append(d)
    return pending


def already_held(db_path: Path, yt_id: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = ?", (f"publish_hold_done_{yt_id}",)
    ).fetchone()
    conn.close()
    return bool(row)


def mark_held(db_path: Path, yt_id: str):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
        (f"publish_hold_done_{yt_id}", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Hold pending publishes to Private (0 quota)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--canal", help="Only process this channel (slug)")
    parser.add_argument("--ids", help="Comma-separated video DB ids to force-process")
    parser.add_argument("--all", action="store_true",
                        help="Process ALL pending scheduled videos (not only past-due)")
    parser.add_argument("--buffer-hours", type=float, default=0.0,
                        help="Hold videos whose target is within this many hours (past-only mode)")
    parser.add_argument("--delay-min", type=int, default=20)
    parser.add_argument("--delay-max", type=int, default=45)
    parser.add_argument("--force", action="store_true",
                        help="Re-hold even if already marked done")
    args = parser.parse_args()

    db_path = _db_path()
    if not db_path.exists():
        logger.error("DB not found: %s — set DATABASE_PATH to the production DB", db_path)
        sys.exit(1)

    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    pending = get_pending(
        db_path, canal=args.canal,
        past_only=not args.all,
        buffer_hours=args.buffer_hours,
        ids=ids,
    )

    if not pending:
        print("No scheduled videos in risk. Nothing to hold.")
        return

    print(f"\n{'='*60}")
    print(f"Vídeos a retener (Privado): {len(pending)}"
          + (" (vencidos/inminentes)" if not args.all else " (todos los pendientes)"))
    for p in pending:
        print(f"  [{p['canal']}] #{p['id']} yt={p['yt_video_id']} tgt={p['target_iso'] or '?'} "
              f"status={p['status']} held={'SI' if already_held(db_path, p['yt_video_id']) else 'no'}")
    print("=" * 60)

    if args.dry_run:
        print("\nDRY RUN — no changes made.")
        return

    # Group by account for efficient browser usage
    from pipeline.youtube_browser import (
        get_browser, close_all_browsers, get_account_for_channel,
    )
    by_account = {}
    for item in pending:
        account = get_account_for_channel(item["canal"])
        if not account:
            logger.warning("No account mapped for %s — skipping #%s",
                           item["canal"], item["yt_video_id"])
            continue
        by_account.setdefault(account, []).append(item)

    total_done = 0
    total_failed = 0
    total_skipped = 0

    for account, items in by_account.items():
        logger.info("Processing %d videos for account: %s", len(items), account)
        browser = get_browser(account)
        consecutive_failures = 0
        for i, item in enumerate(items, 1):
            yt_id = item["yt_video_id"]
            if not args.force and already_held(db_path, yt_id):
                logger.info("[%d/%d] SKIP %s:%s — ya retenido", i, len(items), item["canal"], yt_id)
                total_skipped += 1
                continue

            logger.info("[%d/%d] HOLD %s:%s (tgt=%s)", i, len(items),
                        item["canal"], yt_id, item["target_iso"] or "?")
            success = browser.set_video_private_unschedule(yt_id)
            if success:
                consecutive_failures = 0
                mark_held(db_path, yt_id)
                total_done += 1
                logger.info("[%d/%d] DONE %s:%s", i, len(items), item["canal"], yt_id)
            else:
                total_failed += 1
                logger.warning("[%d/%d] FAILED %s:%s", i, len(items), item["canal"], yt_id)
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    logger.warning("Browser session appears broken — recreating for %s", account)
                    try:
                        browser.close()
                    except Exception:
                        pass
                    close_all_browsers()
                    time.sleep(2)
                    browser = get_browser(account)
                    consecutive_failures = 0

            if i < len(items):
                delay = random.randint(args.delay_min, args.delay_max)
                logger.info("Waiting %ds before next video...", delay)
                time.sleep(delay)

    print(f"\n{'='*60}")
    print(f"RESUMEN: {total_done} retenidos, {total_failed} fallidos, {total_skipped} ya hechos")
    if total_failed:
        print("⚠️  Los fallidos requieren revisión (hold manual en Studio o login del navegador).")
    print("=" * 60)


if __name__ == "__main__":
    main()
