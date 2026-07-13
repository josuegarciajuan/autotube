"""Gamification router — streaks, badges, and content flow."""
from fastapi import APIRouter, Query
from typing import Optional
from api.deps import get_db

router = APIRouter()


@router.get("/streaks")
def get_streaks(channel_id: Optional[int] = Query(None)):
    db = get_db()
    with db._connect() as conn:
        conn.row_factory = None  # Use default row_factory for dict conversion below
        if channel_id:
            rows = conn.execute(
                "SELECT * FROM streaks WHERE channel_id = ? ORDER BY streak_type",
                (channel_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM streaks ORDER BY channel_id, streak_type"
            ).fetchall()
        return [dict(zip([c[0] for c in conn.description], r)) for r in rows]


@router.get("/badges")
def get_badges(channel_id: Optional[int] = Query(None)):
    db = get_db()
    with db._connect() as conn:
        if channel_id:
            rows = conn.execute(
                "SELECT * FROM badges WHERE channel_id = ? ORDER BY unlocked_at DESC",
                (channel_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM badges ORDER BY channel_id, unlocked_at DESC"
            ).fetchall()
        return [dict(zip([c[0] for c in conn.description], r)) for r in rows]


@router.post("/badges/check")
def check_badges(channel_id: Optional[int] = Query(None)):
    """Verify and unlock any pending badges for the channel(s)."""
    db = get_db()
    unlocked = []
    channels = []
    with db._connect() as conn:
        if channel_id:
            channels = [dict(r) for r in conn.execute(
                "SELECT id, name, slug FROM channels WHERE active = 1 AND id = ?",
                (channel_id,)
            ).fetchall()]
        else:
            channels = [dict(r) for r in conn.execute(
                "SELECT id, name, slug FROM channels WHERE active = 1"
            ).fetchall()]

    for ch in channels:
        ch_id = ch["id"]
        with db._connect() as conn:
            # Get existing badges for this channel
            existing = {
                r[0] for r in conn.execute(
                    "SELECT badge_key FROM badges WHERE channel_id = ?", (ch_id,)
                ).fetchall()
            }

            # Check first_blood: any published video
            if "first_blood" not in existing:
                has_video = conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE channel_id = ? AND yt_video_id IS NOT NULL",
                    (ch_id,)
                ).fetchone()[0]
                if has_video > 0:
                    conn.execute(
                        "INSERT OR IGNORE INTO badges (channel_id, badge_key) VALUES (?, ?)",
                        (ch_id, "first_blood")
                    )
                    unlocked.append({"channel_id": ch_id, "badge": "first_blood", "channel_name": ch["name"]})

            # Check centurion: 100+ videos
            if "centurion" not in existing:
                video_count = conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE channel_id = ? AND yt_video_id IS NOT NULL",
                    (ch_id,)
                ).fetchone()[0]
                if video_count >= 100:
                    conn.execute(
                        "INSERT OR IGNORE INTO badges (channel_id, badge_key) VALUES (?, ?)",
                        (ch_id, "centurion")
                    )
                    unlocked.append({"channel_id": ch_id, "badge": "centurion", "channel_name": ch["name"]})

            # Check viral: any video with 10K+ views
            if "viral" not in existing:
                viral_count = conn.execute(
                    """SELECT COUNT(*) FROM videos v
                       JOIN video_stats_history vsh ON vsh.id = (
                           SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                           WHERE vsh2.video_id = v.id AND vsh2.views > 0
                       )
                       WHERE v.channel_id = ? AND vsh.views >= 10000""",
                    (ch_id,)
                ).fetchone()[0]
                if viral_count > 0:
                    conn.execute(
                        "INSERT OR IGNORE INTO badges (channel_id, badge_key) VALUES (?, ?)",
                        (ch_id, "viral")
                    )
                    unlocked.append({"channel_id": ch_id, "badge": "viral", "channel_name": ch["name"]})

            # Check ghost: no activity in 7 days
            if "ghost" not in existing:
                recent_activity = conn.execute(
                    """SELECT COUNT(*) FROM videos v
                       WHERE v.channel_id = ?
                         AND (v.uploaded_at > datetime('now', '-7 days') OR v.created_at > datetime('now', '-7 days'))""",
                    (ch_id,)
                ).fetchone()[0]
                if recent_activity == 0:
                    conn.execute(
                        "INSERT OR IGNORE INTO badges (channel_id, badge_key) VALUES (?, ?)",
                        (ch_id, "ghost")
                    )
                    unlocked.append({"channel_id": ch_id, "badge": "ghost", "channel_name": ch["name"]})

    return {"unlocked": len(unlocked), "badges": unlocked}


@router.get("/events/recent")
def get_recent_events(limit: int = Query(50, ge=1, le=200), channel_id: Optional[int] = Query(None)):
    db = get_db()
    with db._connect() as conn:
        if channel_id:
            rows = conn.execute(
                """SELECT * FROM system_events
                   WHERE channel_id = ? OR channel_id IS NULL
                   ORDER BY created_at DESC LIMIT ?""",
                (channel_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM system_events ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        events = []
        for r in rows:
            d = dict(zip([c[0] for c in conn.description], r))
            if d.get("created_at"):
                d["created_at"] = str(d["created_at"])
            events.append(d)
        return events


@router.get("/channels/{channel_id}/content-flow")
def get_content_flow(channel_id: int):
    """Returns data for Sankey diagram: topics -> scripts -> videos -> views -> revenue."""
    db = get_db()
    with db._connect() as conn:
        conn.row_factory = None
        # Topic count (from content)
        topics = conn.execute(
            "SELECT COUNT(*) FROM content WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0] or 0

        # Scripts count
        scripts = conn.execute(
            "SELECT COUNT(*) FROM scripts WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0] or 0

        # Videos count
        videos = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE channel_id = ? AND yt_video_id IS NOT NULL",
            (channel_id,)
        ).fetchone()[0] or 0

        # Total views
        views = conn.execute(
            """SELECT COALESCE(SUM(vsh.views), 0) FROM videos v
               JOIN video_stats_history vsh ON vsh.id = (
                   SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                   WHERE vsh2.video_id = v.id AND vsh2.views > 0
               )
               WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL""",
            (channel_id,)
        ).fetchone()[0] or 0

        # Total revenue
        revenue = conn.execute(
            """SELECT COALESCE(SUM(csh.estimated_revenue_max), 0)
               FROM channel_stats_history csh
               WHERE csh.channel_id = ?
               ORDER BY csh.fetched_at DESC LIMIT 1""",
            (channel_id,)
        ).fetchone()[0] or 0

        return {
            "channel_id": channel_id,
            "nodes": [
                {"name": "Topics", "value": topics},
                {"name": "Scripts", "value": scripts},
                {"name": "Videos", "value": videos},
                {"name": "Views", "value": views},
                {"name": "Revenue", "value": round(revenue, 2)},
            ],
            "links": [
                {"source": 0, "target": 1, "value": scripts},
                {"source": 1, "target": 2, "value": videos},
                {"source": 2, "target": 3, "value": views},
                {"source": 3, "target": 4, "value": round(revenue, 2)},
            ],
        }
