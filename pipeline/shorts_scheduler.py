"""Shorts scheduler: plans staggered clip publication and native shorts frequency.

Handles two types of scheduling:
1. Clip shorts: staggered after source video upload (N days after, configurable)
2. Native shorts: daily frequency schedule (like regular videos)
"""

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from config.settings import DATABASE_PATH
from config.config_bridge import get_channel_config

logger = logging.getLogger(__name__)

# Default schedule: 1 clip at +1 day, 1 at +2 days, 1 at +3 days
DEFAULT_CLIP_SCHEDULE = [
    {"offset_days": 1, "count": 1},
    {"offset_days": 2, "count": 1},
    {"offset_days": 3, "count": 1},
]
DEFAULT_MAX_CLIPS_PER_VIDEO = 5

# Default native shorts schedule
DEFAULT_NATIVE_SCHEDULE = [
    {"week": 1, "per_day": 1},
    {"week": 2, "per_day": 2},
    {"week": 3, "per_day": 3},
]
DEFAULT_NATIVE_MAX_DAILY = 4


class ShortsScheduler:
    """Manages shorts scheduling and publication."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DATABASE_PATH)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def schedule_clips(
        self, video_id: int, channel_slug: str, clips: list[dict]
    ) -> list[int]:
        """Schedule clip shorts for staggered publication after a video.

        Args:
            video_id: Source video DB ID.
            channel_slug: Channel slug (for config).
            clips: List of clip specs [{start_time, end_time, hook_title, ...}, ...].

        Returns:
            List of created shorts DB IDs.
        """
        if not clips:
            logger.info("No clips to schedule for video #%d", video_id)
            return []

        # Load channel config for schedule
        clip_schedule = DEFAULT_CLIP_SCHEDULE
        max_clips = DEFAULT_MAX_CLIPS_PER_VIDEO
        try:
            ch_config = get_channel_config(channel_slug)
            clip_schedule = getattr(ch_config, "SHORTS_CLIP_SCHEDULE", DEFAULT_CLIP_SCHEDULE)
            max_clips = getattr(ch_config, "SHORTS_MAX_CLIPS_PER_VIDEO", DEFAULT_MAX_CLIPS_PER_VIDEO)
        except Exception:
            pass

        conn = self._get_conn()

        # Get channel_id
        ch = conn.execute(
            "SELECT id FROM channels WHERE slug = ?", (channel_slug,)
        ).fetchone()
        if not ch:
            conn.close()
            return []
        channel_id = ch["id"]

        today = date.today()
        created_ids = []
        clip_index = 0

        for batch in clip_schedule:
            offset = batch.get("offset_days", 1)
            count = batch.get("count", 1)
            scheduled_date = (today + timedelta(days=offset)).isoformat()

            for _ in range(count):
                if clip_index >= len(clips):
                    break
                if clip_index >= max_clips:
                    break

                clip = clips[clip_index]
                cursor = conn.execute(
                    """INSERT INTO shorts
                       (channel_id, source_video_id, type, hook_title, hook_text,
                        start_time, end_time, status, scheduled_date, ranking)
                       VALUES (?, ?, 'clip', ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        channel_id,
                        video_id,
                        clip.get("hook_title", "Sin título")[:60],
                        clip.get("hook_text", "")[:100],
                        clip.get("start_time"),
                        clip.get("end_time"),
                        scheduled_date,
                        clip.get("ranking", clip_index + 1),
                    ),
                )
                created_ids.append(cursor.lastrowid)
                clip_index += 1

        conn.commit()
        conn.close()

        logger.info(
            "Scheduled %d clips for video #%d (channel=%s, dates=%s)",
            len(created_ids),
            video_id,
            channel_slug,
            [f"+{b['offset_days']}d×{b['count']}" for b in clip_schedule[:len(created_ids)]],
        )

        return created_ids

    def get_today_pending(self, channel_id: int = None) -> list[dict]:
        """Get all shorts scheduled for publication today."""
        today = date.today().isoformat()
        conn = self._get_conn()

        q = """SELECT s.*, c.slug as channel_slug, c.name as channel_name,
                      v.titulo_final as source_title
               FROM shorts s
               JOIN channels c ON s.channel_id = c.id
               LEFT JOIN videos v ON s.source_video_id = v.id
               WHERE s.scheduled_date = ?
                 AND s.status = 'pending'"""
        params = [today]

        if channel_id is not None:
            q += " AND s.channel_id = ?"
            params.append(channel_id)

        q += " ORDER BY s.ranking ASC, s.created_at ASC"
        rows = conn.execute(q, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_native_target_today(self, channel_id: int) -> int:
        """Calculate how many native shorts should be produced today for a channel."""
        conn = self._get_conn()

        # Check if we already have a schedule entry for today
        today = date.today().isoformat()
        row = conn.execute(
            "SELECT target_count, produced_count FROM shorts_schedule "
            "WHERE channel_id = ? AND schedule_date = ?",
            (channel_id, today),
        ).fetchone()

        if row:
            conn.close()
            remaining = max(0, row["target_count"] - row["produced_count"])
            return remaining

        # Calculate based on weekly frequency
        # Find the channel's first scheduled date
        first = conn.execute(
            "SELECT MIN(schedule_date) as first_date FROM shorts_schedule WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()

        if first and first["first_date"]:
            first_date = datetime.strptime(first["first_date"], "%Y-%m-%d").date()
            days_since_start = (date.today() - first_date).days
            week_number = (days_since_start // 7) + 1
        else:
            week_number = 1

        # Get native schedule from channel config
        ch_row = conn.execute(
            "SELECT slug FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()

        native_schedule = DEFAULT_NATIVE_SCHEDULE
        max_daily = DEFAULT_NATIVE_MAX_DAILY

        if ch_row:
            try:
                ch_config = get_channel_config(ch_row["slug"])
                native_schedule = getattr(
                    ch_config, "SHORTS_NATIVE_SCHEDULE", DEFAULT_NATIVE_SCHEDULE
                )
                max_daily = getattr(
                    ch_config, "SHORTS_NATIVE_MAX_DAILY", DEFAULT_NATIVE_MAX_DAILY
                )
            except Exception:
                pass

        # Find per_day for current week
        per_day = 1
        for schedule in sorted(native_schedule, key=lambda s: s["week"]):
            if week_number >= schedule["week"]:
                per_day = schedule["per_day"]

        target = min(per_day, max_daily)

        # Create today's schedule entry
        conn.execute(
            """INSERT INTO shorts_schedule (channel_id, schedule_date, target_count)
               VALUES (?, ?, ?)""",
            (channel_id, today, target),
        )
        conn.commit()
        conn.close()

        return target

    def mark_published(self, short_id: int, youtube_id: str, youtube_url: str, file_path: str):
        """Mark a short as published with YouTube metadata."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE shorts
               SET status = 'published',
                   youtube_id = ?,
                   youtube_url = ?,
                   file_path = COALESCE(file_path, ?),
                   published_at = datetime('now','localtime'),
                   updated_at = datetime('now','localtime')
               WHERE id = ?""",
            (youtube_id, youtube_url, file_path, short_id),
        )
        conn.commit()
        conn.close()

    def mark_failed(self, short_id: int, error_message: str):
        """Mark a short as failed."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE shorts
               SET status = 'failed',
                   error_message = ?,
                   updated_at = datetime('now','localtime')
               WHERE id = ?""",
            (error_message[:500], short_id),
        )
        conn.commit()
        conn.close()

    def mark_rendering(self, short_id: int):
        """Mark a short as being rendered."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE shorts SET status = 'rendering', updated_at = datetime('now','localtime') WHERE id = ?",
            (short_id,),
        )
        conn.commit()
        conn.close()

    def mark_ready(self, short_id: int, file_path: str):
        """Mark a short as ready to upload."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE shorts SET status = 'ready', file_path = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (file_path, short_id),
        )
        conn.commit()
        conn.close()

    def get_shorts(
        self,
        channel_id: int = None,
        status: str = None,
        type_filter: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Query shorts with optional filters."""
        conn = self._get_conn()

        q = """SELECT s.*, c.slug as channel_slug, c.name as channel_name,
                      v.titulo_final as source_title
               FROM shorts s
               JOIN channels c ON s.channel_id = c.id
               LEFT JOIN videos v ON s.source_video_id = v.id
               WHERE 1=1"""
        params = []

        if channel_id is not None:
            q += " AND s.channel_id = ?"
            params.append(channel_id)
        if status:
            q += " AND s.status = ?"
            params.append(status)
        if type_filter:
            q += " AND s.type = ?"
            params.append(type_filter)

        q += " ORDER BY s.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(q, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_short(self, short_id: int) -> Optional[dict]:
        """Get a single short by ID."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT s.*, c.slug as channel_slug, c.name as channel_name,
                      v.titulo_final as source_title
               FROM shorts s
               JOIN channels c ON s.channel_id = c.id
               LEFT JOIN videos v ON s.source_video_id = v.id
               WHERE s.id = ?""",
            (short_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_clips_for_video(self, video_id: int) -> list[dict]:
        """Get all clip shorts extracted from a source video."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM shorts WHERE source_video_id = ? AND type = 'clip' ORDER BY ranking",
            (video_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def delete_short(self, short_id: int) -> bool:
        """Delete a short from the database."""
        conn = self._get_conn()
        conn.execute("DELETE FROM shorts WHERE id = ?", (short_id,))
        conn.commit()
        conn.close()
        return True

    def update_short(self, short_id: int, **kwargs) -> bool:
        """Update short metadata."""
        allowed = [
            "hook_title", "hook_text", "title", "status",
            "scheduled_date", "ranking",
        ]
        fields, values = [], []
        for k, v in kwargs.items():
            if k in allowed and v is not None:
                fields.append(f"{k} = ?")
                values.append(v)
        fields.append("updated_at = datetime('now','localtime')")
        values.append(short_id)

        if not fields:
            return False

        conn = self._get_conn()
        conn.execute(f"UPDATE shorts SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()
        return True

    def get_stats(self) -> dict:
        """Get aggregate shorts statistics including YouTube metrics."""
        conn = self._get_conn()

        total = conn.execute("SELECT COUNT(*) as c FROM shorts").fetchone()["c"]
        published = conn.execute(
            "SELECT COUNT(*) as c FROM shorts WHERE status = 'published'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) as c FROM shorts WHERE status = 'pending'"
        ).fetchone()["c"]
        ready = conn.execute(
            "SELECT COUNT(*) as c FROM shorts WHERE status = 'ready'"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM shorts WHERE status = 'failed'"
        ).fetchone()["c"]
        rendering = conn.execute(
            "SELECT COUNT(*) as c FROM shorts WHERE status = 'rendering'"
        ).fetchone()["c"]

        by_type = {}
        for row in conn.execute(
            "SELECT type, COUNT(*) as cnt FROM shorts GROUP BY type"
        ).fetchall():
            by_type[row["type"]] = row["cnt"]

        # YouTube metrics for published shorts
        yt_stats = conn.execute(
            """SELECT COALESCE(SUM(vsh.views), 0) as total_views,
                      COALESCE(SUM(vsh.likes), 0) as total_likes,
                      COALESCE(SUM(vsh.comments), 0) as total_comments
               FROM shorts s
               JOIN video_stats_history vsh ON vsh.yt_video_id = s.youtube_id
                  AND vsh.id = (SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                                WHERE vsh2.yt_video_id = s.youtube_id)
               WHERE s.status = 'published' AND s.youtube_id IS NOT NULL"""
        ).fetchone()

        # Per-channel shorts stats with YouTube metrics
        per_channel = []
        for row in conn.execute(
            """SELECT s.channel_id, c.name as channel_name, c.slug as channel_slug,
                      COUNT(*) as total, SUM(CASE WHEN s.status = 'published' THEN 1 ELSE 0 END) as published,
                      COALESCE(SUM(yt.views), 0) as total_views,
                      COALESCE(SUM(yt.likes), 0) as total_likes
               FROM shorts s
               JOIN channels c ON s.channel_id = c.id
               LEFT JOIN (
                 SELECT s2.id, vsh.views, vsh.likes
                 FROM shorts s2
                 JOIN video_stats_history vsh ON vsh.yt_video_id = s2.youtube_id
                    AND vsh.id = (SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                                  WHERE vsh2.yt_video_id = s2.youtube_id)
                 WHERE s2.status = 'published' AND s2.youtube_id IS NOT NULL
               ) yt ON yt.id = s.id
               GROUP BY s.channel_id
               ORDER BY c.name"""
        ).fetchall():
            per_channel.append(dict(row))

        conn.close()

        # ── Subscribe CTA stats ──
        conn = self._get_conn()
        cta_count = conn.execute(
            "SELECT COUNT(*) as c FROM shorts WHERE has_subscribe_cta = 1 AND status = 'published'"
        ).fetchone()["c"]
        native_published = conn.execute(
            "SELECT COUNT(*) as c FROM shorts WHERE type = 'native' AND status = 'published'"
        ).fetchone()["c"]
        cta_pct = round(cta_count / max(native_published, 1) * 100, 1)
        conn.close()

        return {
            "total": total,
            "published": published,
            "pending": pending,
            "ready": ready,
            "failed": failed,
            "rendering": rendering,
            "by_type": by_type,
            "total_views": yt_stats["total_views"] if yt_stats else 0,
            "total_likes": yt_stats["total_likes"] if yt_stats else 0,
            "total_comments": yt_stats["total_comments"] if yt_stats else 0,
            "per_channel": per_channel,
            "cta_count": cta_count,
            "native_published": native_published,
            "cta_pct": cta_pct,
        }
