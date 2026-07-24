#!/usr/bin/env python3
"""Fix videos where target_public_at is before created_at or uploaded_at.

Problem: Due to timezone confusion and missing staleness checks in the
publish pipeline, some videos ended up with a target_public_at that is
chronologically before their creation or upload time.

This script:
1. Finds all videos with target_public_at < created_at or < uploaded_at
2. Recalculates target_public_at via calculate_target_public_time()
3. Updates videos.target_public_at
4. Updates dependent lifecycle actions (go_public) to match
5. Logs all changes before/after

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


def main():
    parser = argparse.ArgumentParser(
        description="Fix videos with target_public_at before created_at or uploaded_at"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be changed without applying fixes",
    )
    parser.add_argument(
        "--db", type=str, default=DB_PATH,
        help="Path to the SQLite database",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        logger.error("Database not found: %s", args.db)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    try:
        # ── 1. Find stale videos ──
        logger.info("Scanning for videos with stale target_public_at...")
        stale = find_stale_videos(conn, dry_run=args.dry_run)

        if not stale:
            logger.info("No stale targets found — all good!")
            return

        logger.info("Found %d video(s) with stale target_public_at:", len(stale))
        for v in stale:
            logger.info(
                "  #%d [%s] '%s': %s",
                v["id"], v["canal"],
                (v["titulo"] or "?")[:40],
                v["reason"],
            )

        # ── 2. Fix them ──
        mode = "DRY RUN (no changes)" if args.dry_run else "FIXING"
        logger.info("\n%s %d stale target(s)...", mode, len(stale))
        result = fix_stale_targets(conn, stale, dry_run=args.dry_run)

        # ── 3. Summary ──
        logger.info(
            "\nDone: %d checked, %d fixed, %d errors %s",
            result["total_checked"],
            result["fixed"],
            result["errors"],
            "(dry run — no changes applied)" if args.dry_run else "",
        )

        if args.dry_run and result["changes"]:
            logger.info("\nChanges that would be applied:")
            for c in result["changes"]:
                logger.info(
                    "  #%d [%s] %s → %s",
                    c["video_id"], c["canal"],
                    c["old_target"][:19] if c["old_target"] else "None",
                    c["new_target"][:19],
                )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
