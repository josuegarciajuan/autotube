#!/usr/bin/env python3
"""Fix videos and planned_slots where target_public_at is before created_at,
uploaded_at, or target_upload_at.

Problem: Due to timezone confusion and missing staleness checks in the
publish pipeline, some videos ended up with a target_public_at that is
chronologically before their creation, upload, or the planned upload time.

This script:
1. Finds all videos with target_public_at < created_at or < uploaded_at
2. Finds all planned_slots with target_public_at < target_upload_at (any status)
3. Recalculates target_public_at via calculate_target_public_time()
4. Updates videos.target_public_at AND planned_slots.target_public_at
5. Updates dependent lifecycle actions (go_public) to match
6. Logs all changes before/after

Safe to re-run: stale targets that have been fixed will be skipped.
"""

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Ensure project root in path ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("fix_stale_targets")

DB_PATH = "/root/autotube/autotube.db"


def _get_channel_config(conn, channel_id: int) -> dict:
    """Get channel config parsed from config_json."""
    row = conn.execute(
        "SELECT slug, config_json FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    if not row:
        return {}
    cfg = {}
    if row["config_json"]:
        try:
            cfg = json.loads(row["config_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    cfg["slug"] = row["slug"]
    return cfg


def find_stale_videos(conn, dry_run: bool = False) -> list[dict]:
    """Find videos where target_public_at is before created_at or uploaded_at."""
    rows = conn.execute("""
        SELECT v.id, v.channel_id, v.canal, v.target_public_at,
               v.created_at, v.uploaded_at, v.publish_mode, v.status,
               v.titulo_final
        FROM videos v
        WHERE v.target_public_at IS NOT NULL
          AND v.publish_mode = 'scheduled'
          AND v.status NOT IN ('error', 'failed')
        ORDER BY v.channel_id, v.id
    """).fetchall()

    stale = []
    now_utc = datetime.now(timezone.utc)
    for row in rows:
        tpa = row["target_public_at"]
        created = row["created_at"]
        uploaded = row["uploaded_at"]

        # Parse target_public_at
        tpa_dt = _parse_target(str(tpa))
        if tpa_dt is None:
            logger.warning("  Skip video #%d: unparseable target_public_at=%s", row["id"], tpa)
            continue

        # Parse created_at
        created_dt = _parse_target(str(created)) if created else None
        # Parse uploaded_at
        uploaded_dt = _parse_target(str(uploaded)) if uploaded else None

        is_stale = False
        reason = ""

        if created_dt and tpa_dt < created_dt:
            is_stale = True
            reason = f"target_public_at ({tpa_dt.isoformat()}) < created_at ({created_dt.isoformat()})"
        if uploaded_dt and tpa_dt < uploaded_dt:
            is_stale = True
            reason = f"target_public_at ({tpa_dt.isoformat()}) < uploaded_at ({uploaded_dt.isoformat()})"
        if tpa_dt < now_utc and not is_stale:
            # Stale but not necessarily before creation — still worth fixing
            is_stale = True
            reason = f"target_public_at ({tpa_dt.isoformat()}) < now ({now_utc.isoformat()})"

        if is_stale:
            stale.append({
                "id": row["id"],
                "channel_id": row["channel_id"],
                "canal": row["canal"],
                "target_public_at": str(tpa),
                "target_parsed_utc": tpa_dt.isoformat(),
                "created_at": str(created) if created else None,
                "uploaded_at": str(uploaded) if uploaded else None,
                "status": row["status"],
                "titulo": row["titulo_final"],
                "reason": reason,
            })

    return stale


def _parse_target(raw: str):
    """Parse a target_public_at string into a timezone-aware UTC datetime."""
    if not raw:
        return None
    s = str(raw).strip()
    for attempt in [
        # ISO8601 with TZ
        lambda: datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T")),
        # Naive local (fallback: assume Europe/Madrid)
        lambda: _parse_naive_as_utc(s),
    ]:
        try:
            result = attempt()
            if result is not None:
                if result.tzinfo is None:
                    result = result.replace(tzinfo=timezone.utc)
                return result
        except (ValueError, TypeError):
            continue
    return None


def _parse_naive_as_utc(s: str):
    """Parse a naive 'YYYY-MM-DD HH:MM:SS' as Europe/Madrid local → UTC."""
    import pytz
    try:
        tz = pytz.timezone("Europe/Madrid")
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
    naive = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    localized = tz.localize(naive)
    return localized.astimezone(timezone.utc)


def fix_stale_targets(conn, stale_videos: list[dict], dry_run: bool = False) -> dict:
    """Recalculate and fix stale target_public_at for each video."""
    from pipeline.publish_scheduler import calculate_target_public_time

    fixed = 0
    errors = 0
    changes = []

    for v in stale_videos:
        channel_id = v["channel_id"]
        cfg = _get_channel_config(conn, channel_id)
        slug = cfg.get("slug", v.get("canal", "unknown"))

        primary_kw = cfg.get("SEO_PRIMARY_KEYWORD", "")
        secondary_kws = cfg.get("SEO_SECONDARY_KEYWORDS", [])
        tz_str = cfg.get("PUBLISH_TIMEZONE", "Europe/Madrid")
        target_h = cfg.get("PUBLISH_TARGET_HOUR")
        spread = cfg.get("PUBLISH_WINDOW_SPREAD_MIN") or cfg.get("PUBLISH_JITTER_MIN", 20)
        warmup = cfg.get("PUBLISH_WARMUP_MIN", 120)

        try:
            result = calculate_target_public_time(
                slug=slug,
                primary_keyword=primary_kw,
                secondary_keywords=secondary_kws,
                timezone_str=tz_str,
                target_hour=target_h,
                jitter_min=spread,
                warmup_min=warmup,
                channel_id=channel_id,
            )
        except Exception as e:
            logger.error("  FAIL video #%d: calculate_target_public_time error: %s", v["id"], e)
            errors += 1
            continue

        new_target = result["target_public_at"]
        old_target = v["target_public_at"]

        change = {
            "video_id": v["id"],
            "canal": v["canal"],
            "titulo": v["titulo"][:50] if v["titulo"] else "?",
            "old_target": old_target,
            "new_target": new_target,
            "peak_hour": result["peak_hour_local"],
            "peak_source": result["peak_source"],
            "reason": v["reason"],
        }
        changes.append(change)

        logger.info(
            "  #%d [%s] '%s...': %s → %s (peak=%02d:%02d, src=%s)",
            v["id"], v["canal"],
            (v["titulo"] or "?")[:30],
            str(old_target)[:19],
            new_target[:19],
            result["peak_hour_local"],
            0,
            result["peak_source"],
        )

        if not dry_run:
            try:
                # Update video record
                conn.execute(
                    "UPDATE videos SET target_public_at = ?, peak_source = ? WHERE id = ?",
                    (new_target, result["peak_source"], v["id"]),
                )
                # Update lifecycle go_public actions
                conn.execute(
                    """UPDATE video_lifecycle_actions
                       SET scheduled_for = ?
                       WHERE video_id = ? AND action_type = 'go_public' AND status = 'pending'""",
                    (new_target, v["id"]),
                )
                conn.commit()
                fixed += 1
            except Exception as e:
                logger.error("  FAIL video #%d: DB update error: %s", v["id"], e)
                conn.rollback()
                errors += 1

    return {
        "total_checked": len(stale_videos),
        "fixed": fixed,
        "errors": errors,
        "changes": changes,
    }


def find_stale_planned_slots(conn, dry_run: bool = False) -> list[dict]:
    """Find planned_slots where target_public_at < target_upload_at.

    Checks ALL statuses (pending, completed, running, cancelled) because
    completed slots linked to awaiting_upload videos still display in the UI.
    """
    from datetime import timezone as _tz
    import pytz as _pytz

    rows = conn.execute("""
        SELECT ps.id, ps.channel_id, ps.date_key, ps.status,
               ps.target_upload_at, ps.target_public_at,
               ps.video_id,
               c.slug as canal
        FROM planned_slots ps
        JOIN channels c ON ps.channel_id = c.id
        WHERE ps.target_public_at IS NOT NULL
          AND ps.target_upload_at IS NOT NULL
        ORDER BY ps.date_key, c.slug
    """).fetchall()

    stale = []
    tz = _pytz.timezone("Europe/Madrid")
    for row in rows:
        up_str = str(row["target_upload_at"])[:19]
        pub_str = str(row["target_public_at"])

        # Parse upload (always naive local)
        try:
            up_dt = datetime.strptime(up_str, "%Y-%m-%d %H:%M:%S")
            up_utc = tz.localize(up_dt).astimezone(_tz.utc)
        except (ValueError, TypeError):
            continue

        # Parse public (could be ISO8601 UTC or naive local)
        try:
            pub_dt = _parse_target(str(pub_str))
            if pub_dt is None:
                continue
            pub_utc = pub_dt.astimezone(_tz.utc) if pub_dt.tzinfo else tz.localize(pub_dt).astimezone(_tz.utc)
        except (ValueError, TypeError):
            # Fallback naive
            try:
                pub_dt = datetime.strptime(str(pub_str)[:19], "%Y-%m-%d %H:%M:%S")
                pub_utc = tz.localize(pub_dt).astimezone(_tz.utc)
            except:
                continue

        from datetime import timedelta
        # Use channel's warmup from config
        cfg = {}
        try:
            cfg = json.loads(conn.execute(
                'SELECT config_json FROM channels WHERE slug=?', (row['canal'],)
            ).fetchone()['config_json'] or '{}')
        except Exception:
            pass
        warmup = cfg.get('PUBLISH_WARMUP_MIN', 120)
        effective_warmup = warmup + 60  # 60min safety buffer
        min_pub_utc = up_utc + timedelta(minutes=effective_warmup)

        if pub_utc < min_pub_utc:
            gap_min = int((pub_utc - min_pub_utc).total_seconds() / 60)
            stale.append({
                "slot_id": row["id"],
                "channel_id": row["channel_id"],
                "canal": row["canal"],
                "date_key": row["date_key"],
                "slot_status": row["status"],
                "target_public_at": str(pub_str),
                "target_upload_at": up_str,
                "min_pub_utc": min_pub_utc.isoformat(),
                "video_id": row["video_id"],
                "reason": f"pub < up+warmup (gap={gap_min}min)",
            })

    return stale


def fix_stale_planned_slots(conn, stale_slots: list[dict], dry_run: bool = False) -> dict:
    """Fix stale planned_slots by setting target_public_at = target_upload_at + warmup."""
    from datetime import timezone as _tz, timedelta as _td
    import pytz as _pytz

    tz = _pytz.timezone("Europe/Madrid")
    fixed_slots = 0
    fixed_videos = 0
    errors = 0
    changes = []

    for s in stale_slots:
        up_str = s["target_upload_at"]
        try:
            up_dt = datetime.strptime(up_str, "%Y-%m-%d %H:%M:%S")
            up_utc = tz.localize(up_dt).astimezone(_tz.utc)
        except (ValueError, TypeError):
            errors += 1
            continue

        # Use channel's warmup from config plus 60min safety buffer
        cfg = {}
        try:
            cfg = json.loads(conn.execute(
                'SELECT config_json FROM channels WHERE slug=?', (s['canal'],)
            ).fetchone()['config_json'] or '{}')
        except Exception:
            pass
        warmup = cfg.get('PUBLISH_WARMUP_MIN', 120)
        effective_warmup = warmup + 60  # 60min safety buffer

        new_pub_utc = up_utc + _td(minutes=effective_warmup)
        new_pub_str = new_pub_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        old_pub = str(s["target_public_at"])[:19]

        change = {
            "slot_id": s["slot_id"],
            "canal": s["canal"],
            "date_key": s["date_key"],
            "slot_status": s["slot_status"],
            "old_target": old_pub,
            "new_target": new_pub_str[:19],
            "reason": s["reason"],
        }
        changes.append(change)

        logger.info(
            "  slot#%d [%s] %s: %s → %s (%s)",
            s["slot_id"], s["canal"], s["date_key"],
            old_pub, new_pub_str[:19], s["reason"],
        )

        if not dry_run:
            try:
                conn.execute(
                    "UPDATE planned_slots SET target_public_at = ? WHERE id = ?",
                    (new_pub_str, s["slot_id"]),
                )
                fixed_slots += 1

                # Also update linked video if applicable
                if s.get("video_id"):
                    conn.execute(
                        "UPDATE videos SET target_public_at = ? WHERE id = ?",
                        (new_pub_str, s["video_id"]),
                    )
                    # Update lifecycle go_public actions
                    conn.execute(
                        """UPDATE video_lifecycle_actions
                           SET scheduled_for = ?
                           WHERE video_id = ? AND action_type = 'go_public' AND status = 'pending'""",
                        (new_pub_str, s["video_id"]),
                    )
                    fixed_videos += 1

                conn.commit()
            except Exception as e:
                logger.error("  FAIL slot#%d: %s", s["slot_id"], e)
                conn.rollback()
                errors += 1

    return {
        "total_checked": len(stale_slots),
        "slots_fixed": fixed_slots,
        "videos_fixed": fixed_videos,
        "errors": errors,
        "changes": changes,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fix videos and planned_slots with stale target_public_at"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be changed without applying fixes",
    )
    parser.add_argument(
        "--db", type=str, default=DB_PATH,
        help="Path to the SQLite database",
    )
    parser.add_argument(
        "--videos-only", action="store_true",
        help="Only fix videos (skip planned_slots)",
    )
    parser.add_argument(
        "--slots-only", action="store_true",
        help="Only fix planned_slots (skip videos)",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    try:
        total_fixed = 0

        if not args.slots_only:
            # ── 1. Find and fix stale videos ──
            logger.info("Scanning for videos with stale target_public_at...")
            stale_vids = find_stale_videos(conn, dry_run=args.dry_run)

            if stale_vids:
                logger.info("Found %d video(s) with stale target_public_at:", len(stale_vids))
                for v in stale_vids:
                    logger.info(
                        "  #%d [%s] '%s': %s",
                        v["id"], v["canal"],
                        (v["titulo"] or "?")[:40],
                        v["reason"],
                    )

                mode = "DRY RUN (no changes)" if args.dry_run else "FIXING"
                logger.info("\n%s %d stale video target(s)...", mode, len(stale_vids))
                result_vids = fix_stale_targets(conn, stale_vids, dry_run=args.dry_run)
                total_fixed += result_vids["fixed"]

                logger.info(
                    "Videos: %d checked, %d fixed, %d errors %s",
                    result_vids["total_checked"],
                    result_vids["fixed"],
                    result_vids["errors"],
                    "(dry run)" if args.dry_run else "",
                )

                if args.dry_run and result_vids["changes"]:
                    logger.info("\nVideo changes that would be applied:")
                    for c in result_vids["changes"]:
                        logger.info(
                            "  #%d [%s] %s → %s",
                            c["video_id"], c["canal"],
                            c["old_target"][:19] if c["old_target"] else "None",
                            c["new_target"][:19],
                        )
            else:
                logger.info("No stale video targets found.")

        if not args.videos_only:
            # ── 2. Find and fix stale planned_slots ──
            logger.info("\nScanning for planned_slots with stale target_public_at...")
            stale_slots = find_stale_planned_slots(conn, dry_run=args.dry_run)

            if stale_slots:
                logger.info("Found %d planned_slot(s) with stale target_public_at:", len(stale_slots))
                for s in stale_slots:
                    logger.info(
                        "  slot#%d [%s] %s status=%s: %s",
                        s["slot_id"], s["canal"], s["date_key"],
                        s["slot_status"], s["reason"],
                    )

                mode = "DRY RUN (no changes)" if args.dry_run else "FIXING"
                logger.info("\n%s %d stale planned_slot target(s)...", mode, len(stale_slots))
                result_slots = fix_stale_planned_slots(conn, stale_slots, dry_run=args.dry_run)
                total_fixed += result_slots["slots_fixed"]

                logger.info(
                    "Planned_slots: %d checked, %d slots fixed, %d videos fixed, %d errors %s",
                    result_slots["total_checked"],
                    result_slots["slots_fixed"],
                    result_slots["videos_fixed"],
                    result_slots["errors"],
                    "(dry run)" if args.dry_run else "",
                )
            else:
                logger.info("No stale planned_slot targets found.")

        # ── 3. Summary ──
        logger.info(
            "\n╔══════════════════════════════════╗\n"
            "║  Total fixed: %d                  ║\n"
            "╚══════════════════════════════════╝",
            total_fixed,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
