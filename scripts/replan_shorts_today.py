#!/usr/bin/env python3
"""Replan completo de shorts para HOY (operación one-off, no se commitea).

Tras los fixes de timezone (UTC) y la limpieza de huérfanos, regenera los
slots nativos pendientes de hoy para cada canal activo hasta alcanzar su
target configurado (shorts_native_per_day), usando ventanas locales futuras
con gap de 60 min y conversión UTC (scheduled_at = upload - lead 15 min).

Uso:
    python3 scripts/replan_shorts_today.py --dry-run   # previsualizar
    python3 scripts/replan_shorts_today.py --apply     # insertar slots
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("replan_shorts_today")

# Ventanas locales de tarde/noche para lo que queda de hoy (Europe/Madrid)
FALLBACK_WINDOWS = [(13, 30), (16, 0), (18, 30), (21, 0), (22, 30)]
SAME_TYPE_GAP_MIN = 60
GEN_LEAD_MIN = 15


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true",
                        help="Insert slots (default is dry-run)")
    args = parser.parse_args()

    from database.db_extended import ExtendedDatabase
    from config.config_bridge import get_channel_config
    from datetime import timedelta
    from zoneinfo import ZoneInfo as _ZI
    from datetime import timezone as _tz_utc

    db = ExtendedDatabase()
    tz_def = ZoneInfo("Europe/Madrid")
    now_local = datetime.now(tz_def)
    today_local = now_local.date().isoformat()
    now_str_utc = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

    total_created = 0
    for ch in db.get_channels(active_only=True):
        if ch["slug"] == "test":
            continue
        ch_id, slug = ch["id"], ch["slug"]

        cfg = get_channel_config(slug)
        tz = ZoneInfo(getattr(cfg, "PUBLISH_TIMEZONE", "Europe/Madrid"))

        target = db.get_native_shorts_per_day(ch_id)
        published = db.count_native_shorts_published_today(ch_id)
        existing_pending = [
            s for s in db.get_shorts_planned_slots(
                date_key=today_local, channel_id=ch_id, status="pending")
            if s["short_type"] == "native"
        ]
        remaining = max(0, target - published - len(existing_pending))
        if remaining == 0:
            logger.info("[%s] ya cubierto (target=%d pub=%d pend=%d)", slug, target, published, len(existing_pending))
            continue

        # Espaciar en ventanas futuras con gap ≥ 60 min
        last_min = -10**9
        assigned = []
        for (wh, wm) in FALLBACK_WINDOWS:
            if len(assigned) >= remaining:
                break
            minute_of_day = wh * 60 + wm
            if minute_of_day <= now_local.hour * 60 + now_local.minute + 5:
                continue  # ventana ya pasada
            if minute_of_day - last_min < SAME_TYPE_GAP_MIN:
                continue
            last_min = minute_of_day
            assigned.append((wh, wm))

        if len(assigned) < remaining:
            logger.warning("[%s] solo %d ventanas futuras disponibles para %d pendientes", slug, len(assigned), remaining)

        for (wh, wm) in assigned:
            upload_dt = datetime(
                now_local.year, now_local.month, now_local.day, wh, wm, tzinfo=tz
            )
            sched_dt = upload_dt - timedelta(minutes=GEN_LEAD_MIN)
            upload_utc = upload_dt.astimezone(_tz_utc.utc).strftime("%Y-%m-%d %H:%M:%S")
            sched_utc = sched_dt.astimezone(_tz_utc.utc).strftime("%Y-%m-%d %H:%M:%S")
            if sched_utc >= now_str_utc:
                logger.info("[%s] native %02d:%02d local → sched=%s upload=%s", slug, wh, wm, sched_utc, upload_utc)
                if args.apply:
                    db.create_shorts_planned_slots_batch([{
                        "channel_id": ch_id,
                        "date_key": today_local,
                        "scheduled_at": sched_utc,
                        "target_upload_at": upload_utc,
                        "short_type": "native",
                        "status": "pending",
                        "slot_position": 1,
                        "slot_rank": 0,
                    }])
                    total_created += 1

    if args.apply:
        logger.info("Insertados %d slots nativos para hoy.", total_created)
    else:
        logger.info("DRY-RUN — pasa --apply para insertar.")


if __name__ == "__main__":
    main()
