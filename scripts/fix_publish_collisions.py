#!/usr/bin/env python3
"""Fix intra-channel publish time collisions — spread videos across the day.

Problem: Multiple videos from the same channel were all scheduled for the
same target_public_at (e.g., canal3 had 4 videos at 12:54 UTC).

This script:
1. Finds all groups of same-channel videos with identical target_public_at today
2. Spreads them across the channel's peak windows (min 3h apart)
3. Updates videos.target_public_at and all dependent lifecycle actions
4. Marks duplicate unlisted versions as 'superseded'
"""

import sqlite3
import sys
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("fix_collisions")

DB_PATH = "/root/autotube/autotube.db"


def spread_videos_for_channel(conn, channel_id: int, video_ids: list[int],
                               timezone_str: str = "Europe/Madrid") -> dict:
    """Spread video publish times across the day for a channel.
    
    Uses the channel's publish windows (morning 10h, afternoon 14h, 
    evening 18h, night 21h) to distribute videos with minimum 3h gap.
    
    Returns {video_id: new_target_utc_iso}.
    """
    import pytz
    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    
    # Default peak windows for Spain (hour ranges in local time)
    # Each video gets assigned to a different window
    peak_hours = [10, 14, 18, 21]  # CEST times
    # Ensure we have enough peaks
    for i in range(len(peak_hours), len(video_ids)):
        peak_hours.append(22 + (i % 3))  # overflow after 21h
    
    # Sort videos by ID (oldest first gets earliest slot)
    sorted_video_ids = sorted(video_ids)
    
    assignments = {}
    
    for i, video_id in enumerate(sorted_video_ids):
        peak_local_hour = peak_hours[i % len(peak_hours)]
        
        # Add deterministic jitter (±7 min based on video_id)
        jitter = (video_id * 31 + i * 17) % 15 - 7
        
        target_local = now_local.replace(
            hour=peak_local_hour,
            minute=max(0, min(59, 30 + jitter)),
            second=0, microsecond=0
        )
        
        # If the target hour already passed today, push to tomorrow
        if target_local <= now_local + timedelta(minutes=30):
            target_local += timedelta(days=1)
        
        target_utc = target_local.astimezone(pytz.UTC)
        target_iso = target_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        target_plain = target_utc.strftime("%Y-%m-%d %H:%M:%S")
        
        assignments[video_id] = {
            "utc_iso": target_iso,
            "utc_plain": target_plain,
            "local_hour": peak_local_hour,
            "local_time": target_local.strftime("%H:%M"),
        }
    
    return assignments


def fix_channel_collisions(conn, channel_id: int, slug: str) -> int:
    """Fix all same-hour collisions for one channel."""
    
    # Get channel timezone
    tz_str = "Europe/Madrid"
    try:
        row = conn.execute(
            "SELECT slug FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()
    except Exception:
        pass
    
    # Find today's videos with target_public_at that need spreading
    # Group by the hour of target_public_at
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    rows = conn.execute("""
        SELECT v.id, v.target_public_at, v.yt_video_id, v.status,
               v.titulo_final
        FROM videos v
        WHERE v.channel_id = ?
          AND v.status IN ('uploaded_private', 'awaiting_upload')
          AND (v.target_public_at LIKE ? OR v.target_public_at LIKE ?)
          AND v.yt_video_id NOT IN (
              -- Exclude videos that already have an older duplicate
              SELECT v2.yt_video_id FROM videos v2
              WHERE v2.channel_id = ? AND v2.status = 'uploaded_private'
                AND v2.id < v.id AND v2.yt_video_id = v.yt_video_id
          )
        ORDER BY v.id
    """, (channel_id, f"{today}%", f"{today}T%", channel_id)).fetchall()
    
    if not rows:
        logger.info("[%s] No scheduled videos found for today", slug)
        return 0
    
    # Check if all have the same target hour
    hours_seen = set()
    for row in rows:
        tp = row["target_public_at"]
        if not tp:
            continue
        # Extract hour
        try:
            dt = datetime.fromisoformat(tp.replace("Z", "+00:00").replace(" ", "T"))
            hours_seen.add(dt.hour)
        except (ValueError, TypeError):
            pass
    
    video_ids = [r["id"] for r in rows if r["id"] is not None]
    
    if len(hours_seen) <= 1 and len(video_ids) > 1:
        logger.warning(
            "[%s] COLLISION: %d videos all targeting same hour. "
            "Spreading across the day...",
            slug, len(video_ids)
        )
        
        # Spread them
        assignments = spread_videos_for_channel(conn, channel_id, video_ids, tz_str)
        
        updated_videos = 0
        updated_actions = 0
        
        for video_id, assign in assignments.items():
            new_target = assign["utc_plain"]
            new_target_iso = assign["utc_iso"]
            local_time = assign["local_time"]
            
            # Get old target for delta calculation
            old_row = conn.execute(
                "SELECT target_public_at FROM videos WHERE id = ?", (video_id,)
            ).fetchone()
            
            if old_row and old_row["target_public_at"]:
                old_target = old_row["target_public_at"]
            else:
                old_target = None
            
            # Update video
            conn.execute(
                "UPDATE videos SET target_public_at = ? WHERE id = ?",
                (new_target, video_id)
            )
            updated_videos += 1
            
            # Calculate time delta for lifecycle actions
            if old_target:
                try:
                    old_dt = datetime.fromisoformat(
                        old_target.replace("Z", "+00:00").replace(" ", "T")
                    )
                    new_dt = datetime.fromisoformat(
                        new_target.replace(" ", "T")
                    ).replace(tzinfo=timezone.utc)
                    
                    if old_dt.tzinfo is None:
                        old_dt = old_dt.replace(tzinfo=timezone.utc)
                    elif old_dt.tzinfo != timezone.utc:
                        old_dt = old_dt.astimezone(timezone.utc)
                    
                    delta = new_dt - old_dt
                except (ValueError, TypeError) as e:
                    logger.warning("  Video %d: can't parse times, skipping action shift: %s", video_id, e)
                    delta = None
            else:
                delta = None
            
            # Update lifecycle actions — shift all actions relative to go_public
            if delta is not None:
                actions = conn.execute(
                    """SELECT id, action_type, scheduled_for 
                       FROM video_lifecycle_actions 
                       WHERE video_id = ? AND status = 'pending'""",
                    (video_id,)
                ).fetchall()
                
                for action in actions:
                    try:
                        old_sched_dt = datetime.fromisoformat(
                            action["scheduled_for"].replace("Z", "+00:00")
                        )
                        if old_sched_dt.tzinfo is None:
                            old_sched_dt = old_sched_dt.replace(tzinfo=timezone.utc)
                        
                        new_sched_dt = old_sched_dt + delta
                        new_sched_iso = new_sched_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                        
                        conn.execute(
                            "UPDATE video_lifecycle_actions SET scheduled_for = ? WHERE id = ?",
                            (new_sched_iso, action["id"])
                        )
                        updated_actions += 1
                    except (ValueError, TypeError):
                        pass
            
            logger.info(
                "  Video %d → publish at %s (local %s)",
                video_id, new_target_iso, local_time
            )
        
        conn.commit()
        logger.info(
            "[%s] Fixed: %d videos, %d lifecycle actions updated",
            slug, updated_videos, updated_actions
        )
        return updated_videos
    else:
        logger.info("[%s] OK — %d videos, %d different hours", slug, len(video_ids), len(hours_seen))
        return 0


def cleanup_duplicates(conn):
    """Mark older duplicate unlisted videos as 'superseded'."""
    rows = conn.execute("""
        SELECT v1.id as newer_id, v1.yt_video_id, v1.status as new_status,
               v2.id as older_id, v2.status as old_status
        FROM videos v1
        JOIN videos v2 ON v1.yt_video_id = v2.yt_video_id 
            AND v1.channel_id = v2.channel_id
            AND v1.id > v2.id
        WHERE v1.status = 'uploaded_private' 
          AND v2.status IN ('uploaded_private', 'uploaded')
          AND v2.privacy_status = 'unlisted'
    """).fetchall()
    
    cleaned = 0
    for row in rows:
        conn.execute(
            "UPDATE videos SET status = 'superseded' WHERE id = ?",
            (row["older_id"],)
        )
        cleaned += 1
        logger.info(
            "  Cleaned duplicate: old #%d (unlisted) superseded by new #%d (private)",
            row["older_id"], row["newer_id"]
        )
    
    if cleaned:
        conn.commit()
        logger.info("Cleaned %d duplicate videos", cleaned)
    
    return cleaned


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    try:
        # 1. Clean up old duplicate unlisted versions first
        cleanup_duplicates(conn)
        
        # 2. Find all channels with scheduled videos today
        channels = conn.execute(
            "SELECT DISTINCT channel_id FROM videos WHERE status IN ('uploaded_private', 'awaiting_upload')"
        ).fetchall()
        
        total_fixed = 0
        for ch in channels:
            ch_id = ch["channel_id"]
            slug_row = conn.execute(
                "SELECT slug FROM channels WHERE id = ?", (ch_id,)
            ).fetchone()
            slug = slug_row["slug"] if slug_row else f"channel_{ch_id}"
            
            fixed = fix_channel_collisions(conn, ch_id, slug)
            total_fixed += fixed
        
        if total_fixed > 0:
            logger.info("=== TOTAL: %d videos re-scheduled ===", total_fixed)
        else:
            logger.info("=== No collisions found ===")
        
        # 3. Print final state for verification
        rows = conn.execute("""
            SELECT v.channel_id, c.slug, v.id, v.titulo_final, 
                   v.target_public_at, v.status
            FROM videos v
            LEFT JOIN channels c ON v.channel_id = c.id
            WHERE v.status IN ('uploaded_private', 'awaiting_upload')
              AND v.target_public_at IS NOT NULL
            ORDER BY v.channel_id, v.target_public_at
        """).fetchall()
        
        print("\n=== Final state ===")
        for row in rows:
            print(f"  [{row['slug']}] #{row['id']}: go_public={row['target_public_at']} | {row['titulo_final'][:60] if row['titulo_final'] else '?'}")
    
    finally:
        conn.close()


if __name__ == "__main__":
    main()
