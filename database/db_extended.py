"""Extended Database class for Autotube v2 panel.

Adds channel management, video scene tracking, and generation jobs
on top of the existing Database class.
"""

import json
import sqlite3
import os
from pathlib import Path
from typing import Optional

from config.settings import DATABASE_PATH
from database.db import Database, init_db as _init_db


SCHEMA_V2_PATH = Path(__file__).parent / "schema_v2.sql"


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def normalize_media_paths(conn, logger):
    """Idempotent: normalize video_path, thumbnail_path, and image_path to
    project-root-relative form (e.g. 'output/videos/foo.mp4').

    Converts absolute paths under the project root (like
    /root/autotube/output/...) to relative form, removes duplicate
    'output/' prefixes, and leaves external paths (/tmp/...) as-is.
    """
    pr = str(PROJECT_ROOT)

    def _normalize(stored):
        if not stored or not isinstance(stored, str):
            return stored
        # Already relative: nothing to do.
        if not stored.startswith("/"):
            # Strip accidental 'output/output/…' double prefix
            if stored.startswith("output/output/"):
                return stored[len("output/"):]
            return stored
        # Absolute path under project root → relativize
        if stored.startswith(pr):
            return Path(stored).relative_to(pr).as_posix()
        # External absolute path (e.g. /tmp/…) → keep as-is
        return stored

    updates = 0
    for table, col, id_col in [
        ("videos", "video_path", "id"),
        ("videos", "thumbnail_path", "id"),
        ("scenes", "image_path", "id"),
    ]:
        # Skip tables that don't exist yet
        exists = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        if not exists:
            continue

        rows = conn.execute(
            f"SELECT {id_col}, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != '' AND {col} != 'pending'"
        ).fetchall()
        for row in rows:
            old = row[col]
            new = _normalize(old)
            if new != old:
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE {id_col} = ?",
                    (new, row[id_col]),
                )
                updates += 1

    if updates:
        logger.info("Migration: normalized %d media paths to relative form", updates)


def migrate_v2(db_path: str = None):
    """Run v2 schema migration (idempotent)."""
    import logging
    logger = logging.getLogger(__name__)
    
    if db_path is None:
        db_path = str(DATABASE_PATH)
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Run new schema
    if SCHEMA_V2_PATH.exists():
        with open(SCHEMA_V2_PATH) as f:
            conn.executescript(f.read())
    
    # Add new columns to videos (idempotent via try/except)
    new_columns = [
        ("status", "TEXT DEFAULT 'draft'"),
        ("progress", "INTEGER DEFAULT 0"),
        ("progress_phase", "TEXT"),
        ("description", "TEXT"),
        ("tags_json", "TEXT"),
        ("title_options", "TEXT"),
        ("channel_id", "INTEGER REFERENCES channels(id)"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    for col_name, col_def in new_columns:
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                pass

    # Add bloques_json column to scripts (v2 migration)
    existing_scripts = {row[1] for row in conn.execute("PRAGMA table_info(scripts)").fetchall()}
    if "bloques_json" not in existing_scripts:
        try:
            conn.execute("ALTER TABLE scripts ADD COLUMN bloques_json TEXT")
            logger.info("Migration: added bloques_json column to scripts")
        except sqlite3.OperationalError:
            pass

    # Add profile columns to channels (idempotent)
    profile_columns = [
        ("description", "TEXT"),
        ("banner_url", "TEXT"),
        ("avatar_url", "TEXT"),
        ("yt_channel_id", "TEXT"),
        ("yt_channel_url", "TEXT"),
    ]
    existing_ch = {row[1] for row in conn.execute("PRAGMA table_info(channels)").fetchall()}
    for col_name, col_def in profile_columns:
        if col_name not in existing_ch:
            try:
                conn.execute(f"ALTER TABLE channels ADD COLUMN {col_name} {col_def}")
                logger.info("Migration: added %s column to channels", col_name)
            except sqlite3.OperationalError:
                pass

    # Run v3 schema (stats history tables)
    schema_v3 = Path(__file__).parent / "schema_v3.sql"
    if schema_v3.exists():
        with open(schema_v3) as f:
            conn.executescript(f.read())
        logger.info("Migration: v3 schema applied")
    
    # Seed canal1 if channels table is empty
    row = conn.execute("SELECT COUNT(*) as cnt FROM channels").fetchone()
    if row["cnt"] == 0:
        conn.execute(
            "INSERT INTO channels (name, slug, config_json, active) VALUES (?, ?, ?, ?)",
            ("Psicología Oculta", "canal1", "{}", 1),
        )
        # Link existing videos to channel 1
        conn.execute("UPDATE videos SET channel_id = 1 WHERE channel_id IS NULL")

    # Seed canal2 if it doesn't exist yet
    row2 = conn.execute(
        "SELECT COUNT(*) as cnt FROM channels WHERE slug = 'canal2'"
    ).fetchone()
    canal2_is_new = row2["cnt"] == 0
    if canal2_is_new:
        conn.execute(
            "INSERT INTO channels (name, slug, config_json, active) VALUES (?, ?, ?, ?)",
            ("Sincronías", "canal2", "{}", 1),
        )

    # Check if canal2 needs profile seeding (new OR missing description)
    canal2_needs_profile = canal2_is_new
    if not canal2_is_new:
        prof = conn.execute(
            "SELECT description, banner_url, avatar_url FROM channels WHERE slug = 'canal2'"
        ).fetchone()
        if prof and not any([prof["description"], prof["banner_url"], prof["avatar_url"]]):
            canal2_needs_profile = True

    # ── v4: Normalize media paths to project-root-relative form ──
    normalize_media_paths(conn, logger)

    conn.commit()
    conn.close()

    # Generate channel profile (description, banner, avatar) for canal2 if needed
    if canal2_needs_profile:
        _seed_canal2_profile()

    # Ensure content_schedules table exists (idempotent — adds video_id column if missing)
    _migrate_content_schedules(db_path)


def _seed_canal2_profile():
    """Generate banner, avatar, and description for canal2 on first creation."""
    import logging
    _log = logging.getLogger(__name__)

    try:
        from pipeline.channel_profile_generator import generate_channel_profile
        from config.settings import DATABASE_PATH
        import sqlite3

        _log.info("Generating channel profile for canal2...")
        profile = generate_channel_profile("canal2")

        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.execute(
            """UPDATE channels
               SET description = ?,
                   banner_url = ?,
                   avatar_url = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE slug = 'canal2'""",
            (profile["description"], profile["banner_url"], profile["avatar_url"]),
        )
        conn.commit()
        conn.close()

        _log.info(
            "canal2 profile seeded: desc=%d chars, banner=%s, avatar=%s",
            len(profile["description"]),
            profile["banner_url"][-40:] if profile["banner_url"] else "(none)",
            profile["avatar_url"][-40:] if profile["avatar_url"] else "(none)",
        )

    except Exception as exc:
        _log.warning("Failed to seed canal2 profile: %s", exc)


def _migrate_content_schedules(db_path: str = None):
    """Ensure content_schedules table exists with correct schema (idempotent)."""
    import logging
    _log = logging.getLogger(__name__)
    
    if db_path is None:
        from config.settings import DATABASE_PATH
        db_path = str(DATABASE_PATH)
    
    conn = sqlite3.connect(db_path)
    
    # Create table if it doesn't exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS content_schedules (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id    INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            content_id    INTEGER REFERENCES raw_content(id) ON DELETE SET NULL,
            action        TEXT NOT NULL DEFAULT 'generate_and_upload',
            schedule_type TEXT NOT NULL DEFAULT 'recurring',
            interval_h    INTEGER DEFAULT 24,
            next_run_at   TIMESTAMP,
            last_run_at   TIMESTAMP,
            video_id      INTEGER REFERENCES videos(id) ON DELETE SET NULL,
            active        BOOLEAN DEFAULT 1,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_next ON content_schedules(active, next_run_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_channel ON content_schedules(channel_id)")
    
    # Add video_id column to existing tables (idempotent)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(content_schedules)").fetchall()}
    if "video_id" not in existing:
        try:
            conn.execute("ALTER TABLE content_schedules ADD COLUMN video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL")
            _log.info("Migration: added video_id column to content_schedules")
        except sqlite3.OperationalError:
            pass
    
    conn.commit()
    conn.close()


class ExtendedDatabase(Database):
    """Extended DB with channel, video scene, and job management."""
    
    # ── Channels ──────────────────────────────────────────────
    
    def create_channel(self, name: str, slug: str, config: dict = None) -> Optional[int]:
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO channels (name, slug, config_json) VALUES (?, ?, ?)",
                    (name, slug, json.dumps(config or {}, ensure_ascii=False)),
                )
                conn.commit()
                ch_id = cursor.lastrowid
                # Create output dirs for this channel
                from config.settings import OUTPUT_DIR
                for d in ["audio", "images", "videos", "thumbnails"]:
                    (OUTPUT_DIR / d / slug).mkdir(parents=True, exist_ok=True)
                return ch_id
            except sqlite3.IntegrityError:
                return None
    
    def update_channel(self, channel_id: int, name: str = None, slug: str = None, 
                       config: dict = None, active: bool = None) -> bool:
        fields, values = [], []
        if name is not None:
            fields.append("name = ?"); values.append(name)
            fields.append("updated_at = CURRENT_TIMESTAMP")
        if slug is not None:
            fields.append("slug = ?"); values.append(slug)
            fields.append("updated_at = CURRENT_TIMESTAMP")
        if config is not None:
            fields.append("config_json = ?"); values.append(json.dumps(config, ensure_ascii=False))
            fields.append("updated_at = CURRENT_TIMESTAMP")
        if active is not None:
            fields.append("active = ?"); values.append(active)
        if not fields:
            return False
        values.append(channel_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE channels SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True

    def update_channel_profile_fields(
        self,
        channel_id: int,
        description: str = None,
        banner_url: str = None,
        avatar_url: str = None,
        yt_channel_id: str = None,
        yt_channel_url: str = None,
    ) -> bool:
        """Update channel visual profile fields (description, banner, avatar)."""
        fields, values = [], []
        if description is not None:
            fields.append("description = ?"); values.append(description)
        if banner_url is not None:
            fields.append("banner_url = ?"); values.append(banner_url)
        if avatar_url is not None:
            fields.append("avatar_url = ?"); values.append(avatar_url)
        if yt_channel_id is not None:
            fields.append("yt_channel_id = ?"); values.append(yt_channel_id)
        if yt_channel_url is not None:
            fields.append("yt_channel_url = ?"); values.append(yt_channel_url)
        if not fields:
            return False
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(channel_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE channels SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True
    
    def delete_channel(self, channel_id: int) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM video_scenes WHERE video_id IN (SELECT id FROM videos WHERE channel_id = ?)", (channel_id,))
            conn.execute("DELETE FROM videos WHERE channel_id = ?", (channel_id,))
            conn.execute("DELETE FROM generation_jobs WHERE channel_id = ?", (channel_id,))
            conn.execute("DELETE FROM content_schedules WHERE channel_id = ?", (channel_id,))
            conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
            conn.commit()
        return True
    
    def get_channel(self, channel_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        return dict(row) if row else None
    
    def get_channels(self, active_only: bool = False) -> list[dict]:
        with self._connect() as conn:
            q = "SELECT * FROM channels"
            if active_only:
                q += " WHERE active = 1"
            q += " ORDER BY created_at DESC"
            rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]
    
    def get_channel_by_slug(self, slug: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None
    
    # ── Videos (extended) ────────────────────────────────────
    
    def get_videos(self, channel_id: int = None, status: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._connect() as conn:
            q = "SELECT v.*, c.name as channel_name FROM videos v LEFT JOIN channels c ON v.channel_id = c.id WHERE 1=1"
            params = []
            if channel_id is not None:
                q += " AND v.channel_id = ?"
                params.append(channel_id)
            if status:
                q += " AND v.status = ?"
                params.append(status)
            q += " ORDER BY v.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    
    def get_video(self, video_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT v.*, c.name as channel_name "
                "FROM videos v LEFT JOIN channels c ON v.channel_id = c.id "
                "WHERE v.id = ?", (video_id,)
            ).fetchone()
        return dict(row) if row else None
    
    def update_video(self, video_id: int, **kwargs) -> bool:
        allowed = ["titulo_final", "description", "tags_json", "title_options",
                    "privacy_status", "status", "progress", "progress_phase",
                    "video_path", "thumbnail_path", "audio_path", "duracion_seg",
                    "script_id", "channel_id"]
        fields, values = [], []
        for k, v in kwargs.items():
            if k in allowed and v is not None:
                fields.append(f"{k} = ?")
                if isinstance(v, (list, dict)):
                    values.append(json.dumps(v, ensure_ascii=False))
                else:
                    values.append(v)
        if not fields:
            return False
        values.append(video_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE videos SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True
    
    def mark_video_uploaded(self, video_id, yt_video_id, yt_url):
        with self._connect() as conn:
            conn.execute(
                "UPDATE videos SET yt_video_id=?, yt_url=?, uploaded_at=CURRENT_TIMESTAMP, status='uploaded' WHERE id=?",
                (yt_video_id, yt_url, video_id),
            )
            conn.commit()
    
    def delete_video(self, video_id: int) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM video_scenes WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            conn.commit()
        return True
    
    # ── Video Scenes ─────────────────────────────────────────
    
    def insert_scenes_batch(self, video_id: int, scenes: list[dict]):
        with self._connect() as conn:
            for i, scene in enumerate(scenes):
                conn.execute(
                    """INSERT INTO video_scenes 
                       (video_id, scene_order, description, script_text, audio_path, 
                        image_path, image_url, subtitle_text, duration_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (video_id, i, scene.get("description"), scene.get("script_text"),
                     scene.get("audio_path"), scene.get("image_path"), scene.get("image_url"),
                     scene.get("subtitle_text"), scene.get("duration_ms")),
                )
            conn.commit()
    
    def get_scenes(self, video_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM video_scenes WHERE video_id = ? ORDER BY scene_order",
                (video_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    
    def update_scene(self, scene_id: int, **kwargs) -> bool:
        allowed = ["script_text", "description", "image_path", "image_url",
                    "audio_path", "subtitle_text", "duration_ms", "scene_order"]
        fields, values = [], []
        for k, v in kwargs.items():
            if k in allowed and v is not None:
                fields.append(f"{k} = ?")
                values.append(v)
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(scene_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE video_scenes SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True
    
    # ── Generation Jobs ──────────────────────────────────────
    
    def create_job(self, channel_id: int, action: str, video_id: int = None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO generation_jobs (channel_id, video_id, action) VALUES (?, ?, ?)",
                (channel_id, video_id, action),
            )
            conn.commit()
            return cursor.lastrowid
    
    def update_job(self, job_id: int, **kwargs) -> bool:
        allowed = ["status", "progress", "phase", "error_msg", "video_id"]
        fields, values = [], []
        for k, v in kwargs.items():
            if k in allowed and v is not None:
                fields.append(f"{k} = ?")
                values.append(v)
        if kwargs.get("status") == "running" and "started_at" not in kwargs:
            fields.append("started_at = COALESCE(started_at, CURRENT_TIMESTAMP)")
        if kwargs.get("status") in ("completed", "failed"):
            fields.append("finished_at = CURRENT_TIMESTAMP")
        values.append(job_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE generation_jobs SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True
    
    def get_job(self, job_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM generation_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    
    def get_active_jobs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM generation_jobs WHERE status IN ('queued','running') ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    
    def get_channel_jobs(self, channel_id: int, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM generation_jobs WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    
    # ── Stats ────────────────────────────────────────────────
    
    def get_dashboard_stats(self) -> dict:
        with self._connect() as conn:
            channels = conn.execute("SELECT COUNT(*) as c FROM channels WHERE active=1").fetchone()["c"]
            total_videos = conn.execute("SELECT COUNT(*) as c FROM videos").fetchone()["c"]
            uploaded = conn.execute("SELECT COUNT(*) as c FROM videos WHERE yt_video_id IS NOT NULL").fetchone()["c"]
            generating = conn.execute("SELECT COUNT(*) as c FROM videos WHERE status='generating'").fetchone()["c"]
            ready = conn.execute("SELECT COUNT(*) as c FROM videos WHERE status='ready'").fetchone()["c"]
            content = conn.execute("SELECT COUNT(*) as c FROM raw_content WHERE used=0").fetchone()["c"]
            scripts = conn.execute("SELECT COUNT(*) as c FROM scripts WHERE used=0").fetchone()["c"]
        
        return {
            "channels": channels,
            "total_videos": total_videos,
            "uploaded_videos": uploaded,
            "generating_videos": generating,
            "ready_videos": ready,
            "unused_content": content,
            "unused_scripts": scripts,
        }
    
    def get_pipeline_logs(self, channel_id: int = None, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            if channel_id:
                canal = self.get_channel(channel_id)
                canal_name = canal["slug"] if canal else "canal1"
                rows = conn.execute(
                    "SELECT * FROM pipeline_log WHERE canal = ? ORDER BY created_at DESC LIMIT ?",
                    (canal_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pipeline_log ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    # ── Video Stats History ────────────────────────────────────

    def insert_video_stats(self, video_id: int, yt_video_id: str, stats: dict) -> int | None:
        """Insert a snapshot of YouTube video statistics."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO video_stats_history
                   (video_id, yt_video_id, views, likes, comments,
                    estimated_minutes_watched, average_view_duration,
                    subscribers_gained)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    video_id,
                    yt_video_id,
                    int(stats.get("viewCount", 0)),
                    int(stats.get("likeCount", 0)),
                    int(stats.get("commentCount", 0)),
                    float(stats.get("estimatedMinutesWatched", 0)),
                    float(stats.get("averageViewDuration", 0)),
                    int(stats.get("subscribersGained", 0)),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_video_stats_history(self, video_id: int, days: int = 30) -> list[dict]:
        """Get stats history for a video over the last N days."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM video_stats_history
                   WHERE video_id = ?
                     AND fetched_at >= datetime('now', ?)
                   ORDER BY fetched_at ASC""",
                (video_id, f"-{days} days"),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Channel Stats History ──────────────────────────────────

    def insert_channel_stats(self, channel_id: int, stats: dict) -> int | None:
        """Insert a snapshot of YouTube channel statistics."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO channel_stats_history
                   (channel_id, subscribers, total_views, video_count)
                   VALUES (?, ?, ?, ?)""",
                (
                    channel_id,
                    int(stats.get("subscriberCount", 0)),
                    int(stats.get("viewCount", 0)),
                    int(stats.get("videoCount", 0)),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_channel_stats_history(self, channel_id: int, days: int = 30) -> list[dict]:
        """Get stats history for a channel over the last N days."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM channel_stats_history
                   WHERE channel_id = ?
                     AND fetched_at >= datetime('now', ?)
                   ORDER BY fetched_at ASC""",
                (channel_id, f"-{days} days"),
            ).fetchall()
        return [dict(r) for r in rows]
