"""Extended Database class for Autotube v2 panel.

Adds channel management, video scene tracking, and generation jobs
on top of the existing Database class.
"""

import importlib
import json
import logging
import sqlite3
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.settings import DATABASE_PATH
from database.db import Database, init_db as _init_db

logger = logging.getLogger(__name__)


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
    
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    
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
        ("checkpoint_data", "TEXT DEFAULT '{}'"),
        ("timing_data", "TEXT DEFAULT '{}'"),
        # ── Scheduled publishing (v7 migration) ──
        ("publish_mode", "TEXT DEFAULT 'immediate'"),
        ("target_public_at", "TIMESTAMP"),
        ("published_at", "TIMESTAMP"),
        ("peak_source", "TEXT"),
        ("auto_playlist_id", "INTEGER"),
        ("auto_playlist_name", "TEXT"),
        ("target_playlist_id", "INTEGER REFERENCES youtube_playlists(id)"),
        ("target_playlist_slug", "TEXT"),
        ("manual_altered_content_done", "INTEGER DEFAULT 0"),
        ("manual_end_screens_done", "INTEGER DEFAULT 0"),
        # ── Generation lifecycle timestamps ──
        ("generation_started_at", "TIMESTAMP"),
        ("generation_finished_at", "TIMESTAMP"),
        ("scheduled_upload_at", "TEXT"),
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

    # Run v3 schema (stats history tables + channel_templates)
    schema_v3 = Path(__file__).parent / "schema_v3.sql"
    if schema_v3.exists():
        with open(schema_v3) as f:
            conn.executescript(f.read())
        logger.info("Migration: v3 schema applied")

    # Run v4 schema (shorts tables)
    schema_v4 = Path(__file__).parent / "schema_v4.sql"
    if schema_v4.exists():
        with open(schema_v4) as f:
            conn.executescript(f.read())
        logger.info("Migration: v4 schema applied")

    # Run v5 schema (promotion/lifecycle tables)
    schema_v5 = Path(__file__).parent / "schema_v5.sql"
    if schema_v5.exists():
        with open(schema_v5) as f:
            conn.executescript(f.read())
        logger.info("Migration: v5 schema applied")

    # Run v6 schema (viral mirror support) — idempotent per-column ALTER
    _migrate_v6(conn, logger)

    # Run v7 schema (channel_daily_watchtime for YPP monetization tracking)
    _migrate_v7(conn, logger)

    # Run v8 schema (streaks, badges, system_events for dashboard v3)
    schema_v8 = Path(__file__).parent / "schema_v8.sql"
    if schema_v8.exists():
        with open(schema_v8) as f:
            conn.executescript(f.read())
        logger.info("Migration: v8 schema applied")

    # Run v9 schema (video_asset_history cross-video dedup)
    schema_v9 = Path(__file__).parent / "schema_v9.sql"
    if schema_v9.exists():
        with open(schema_v9) as f:
            conn.executescript(f.read())
        logger.info("Migration: v9 schema applied")

    # Run v10 schema (Smart Scheduling v2: pipeline_phase + peak_ram_mb)
    # Idempotent column additions (ALTER TABLE in SQLite is not idempotent)
    existing_gj_v10 = {row[1] for row in conn.execute("PRAGMA table_info(generation_jobs)").fetchall()}
    v10_gj_columns = [
        ("pipeline_phase", "TEXT DEFAULT NULL"),
    ]
    for col_name, col_def in v10_gj_columns:
        if col_name not in existing_gj_v10:
            try:
                conn.execute(f"ALTER TABLE generation_jobs ADD COLUMN {col_name} {col_def}")
                logger.info("Migration v10: added %s column to generation_jobs", col_name)
            except sqlite3.OperationalError:
                pass

    existing_v_v10 = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    v10_v_columns = [
        ("peak_ram_mb", "INTEGER DEFAULT NULL"),
    ]
    for col_name, col_def in v10_v_columns:
        if col_name not in existing_v_v10:
            try:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col_name} {col_def}")
                logger.info("Migration v10: added %s column to videos", col_name)
            except sqlite3.OperationalError:
                pass

    # Index for pipeline_phase lookups (idempotent via IF NOT EXISTS)
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_pipeline_phase "
            "ON generation_jobs(pipeline_phase, status)"
        )
    except Exception:
        pass

    # ── channel_tts_lock: cross-process mutex for Kokoro TTS ──
    # Prevents concurrent Kokoro TTS workers on the same channel,
    # which would cause RTF degradation and 600s timeouts.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_tts_lock (
            channel_id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL,
            locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
        )
    """)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tts_lock_channel ON channel_tts_lock(channel_id)")
    except Exception:
        pass

    # ── v4.1: Migrate shorts_planning_config to new column schema ──
    _migrate_shorts_planning_config(conn, logger)
    
    # Add target_time column to shorts_schedule (idempotent)
    existing_ss = {row[1] for row in conn.execute("PRAGMA table_info(shorts_schedule)").fetchall()}
    if "target_time" not in existing_ss:
        try:
            conn.execute("ALTER TABLE shorts_schedule ADD COLUMN target_time TEXT")
            logger.info("Migration: added target_time column to shorts_schedule")
        except sqlite3.OperationalError:
            pass

    # Add heartbeat + retry columns to generation_jobs (idempotent)
    existing_gj = {row[1] for row in conn.execute("PRAGMA table_info(generation_jobs)").fetchall()}
    gj_columns = [
        ("last_heartbeat_at", "TIMESTAMP"),
        ("retry_count", "INTEGER DEFAULT 0"),
        ("worker_pid", "INTEGER"),
    ]
    for col_name, col_def in gj_columns:
        if col_name not in existing_gj:
            try:
                conn.execute(f"ALTER TABLE generation_jobs ADD COLUMN {col_name} {col_def}")
                logger.info("Migration: added %s column to generation_jobs", col_name)
            except sqlite3.OperationalError:
                pass

    # Seed shorts_planning_config for existing channels
    channels = conn.execute("SELECT id, slug FROM channels WHERE active = 1").fetchall()
    for ch in channels:
        exists = conn.execute(
            "SELECT COUNT(*) as c FROM shorts_planning_config WHERE channel_id = ?",
            (ch["id"],),
        ).fetchone()
        if exists["c"] == 0:
            # All channels: 3 native + 3 clips per long video (v12 dynamic scaling)
            conn.execute(
                """INSERT INTO shorts_planning_config
                   (channel_id, shorts_native_per_day, shorts_clip_per_day, shorts_clips_per_long, shorts_enabled)
                   VALUES (?, 3, 2, 3, 1)""",
                (ch["id"],),
            )
    conn.commit()
    
    # ── v12 one-time: bump all active channels to 3 native + 3 clips_per_long ──
    # Only updates if the old value was different (safe for first run after v12 deploy)
    conn.execute("""
        UPDATE shorts_planning_config
        SET shorts_native_per_day = 3,
            shorts_clips_per_long = COALESCE(shorts_clips_per_long, 3)
        WHERE shorts_native_per_day < 3
           OR shorts_clips_per_long IS NULL
    """)
    conn.commit()
    
    # Add topic column to shorts for native short topic deduplication (idempotent)
    existing_shorts = {row[1] for row in conn.execute("PRAGMA table_info(shorts)").fetchall()}
    if "topic" not in existing_shorts:
        try:
            conn.execute("ALTER TABLE shorts ADD COLUMN topic TEXT")
            logger.info("Migration: added topic column to shorts")
        except sqlite3.OperationalError:
            pass

    # Add estimated_minutes_watched to channel_stats_history (idempotent)
    existing_csh = {row[1] for row in conn.execute("PRAGMA table_info(channel_stats_history)").fetchall()}
    if "estimated_minutes_watched" not in existing_csh:
        try:
            conn.execute("ALTER TABLE channel_stats_history ADD COLUMN estimated_minutes_watched REAL DEFAULT 0")
            logger.info("Migration: added estimated_minutes_watched column to channel_stats_history")
        except sqlite3.OperationalError:
            pass

    # Create short_stats table for tracking shorts YouTube metrics (v5 migration)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS short_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            short_id INTEGER NOT NULL REFERENCES shorts(id) ON DELETE CASCADE,
            yt_video_id TEXT NOT NULL,
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            estimated_minutes_watched REAL DEFAULT 0,
            average_view_duration REAL DEFAULT 0,
            embeddable INTEGER DEFAULT 1,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ss_short ON short_stats(short_id, fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ss_ytid ON short_stats(yt_video_id, fetched_at)")
    logger.info("Migration: short_stats table ensured")

    # Add embeddable column to video_stats_history (idempotent)
    existing_vsh = {row[1] for row in conn.execute("PRAGMA table_info(video_stats_history)").fetchall()}
    if "embeddable" not in existing_vsh:
        try:
            conn.execute("ALTER TABLE video_stats_history ADD COLUMN embeddable INTEGER DEFAULT 1")
            logger.info("Migration: added embeddable column to video_stats_history")
        except sqlite3.OperationalError:
            pass

    # Add embeddable column to short_stats (idempotent)
    existing_ss_cols = {row[1] for row in conn.execute("PRAGMA table_info(short_stats)").fetchall()}
    if "embeddable" not in existing_ss_cols:
        try:
            conn.execute("ALTER TABLE short_stats ADD COLUMN embeddable INTEGER DEFAULT 1")
            logger.info("Migration: added embeddable column to short_stats")
        except sqlite3.OperationalError:
            pass

    # Ensure channel_templates table exists (idempotent — may also be in schema_v3.sql)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            segment_type TEXT NOT NULL CHECK(segment_type IN ('intro', 'cta', 'outro')),
            video_path TEXT,
            image_path TEXT,
            config_json TEXT,
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
            UNIQUE(channel_id, segment_type)
        )
    """)
    
    # ── v6: shorts_planned_slots table for per-slot shorts scheduling ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shorts_planned_slots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            date_key            TEXT NOT NULL,
            scheduled_at        TIMESTAMP NOT NULL,
            target_upload_at    TIMESTAMP,
            status              TEXT NOT NULL DEFAULT 'pending',
            short_type          TEXT NOT NULL DEFAULT 'native',
            slot_position       INTEGER DEFAULT 0,
            long_slot_position  INTEGER,
            source_video_id     INTEGER REFERENCES videos(id) ON DELETE SET NULL,
            short_id            INTEGER REFERENCES shorts(id) ON DELETE SET NULL,
            job_id              INTEGER REFERENCES generation_jobs(id) ON DELETE SET NULL,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sps_date ON shorts_planned_slots(date_key, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sps_channel ON shorts_planned_slots(channel_id, date_key)")
    logger.info("Migration: shorts_planned_slots table ensured")

    # ── system_state: simple key-value persistence for in-memory state ──
    # Used by stats collector to survive API restarts (last_collection ts, collection state)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL DEFAULT '',
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    logger.info("Migration: system_state table ensured")

    # Seed channels from active channel configs (idempotent — only inserts missing slugs)
    from config.settings import ACTIVE_CHANNELS
    channel_seeds = {}
    for slug in ACTIVE_CHANNELS:
        try:
            mod = importlib.import_module(f"config.{slug}_config")
            name = getattr(mod, "CANAL_DISPLAY_NAME", slug.capitalize())
            channel_seeds[slug] = name
        except ImportError:
            logger.warning("No config module for slug=%s, skipping seed", slug)
    
    # Insert any missing channels
    for slug, name in channel_seeds.items():
        exists = conn.execute(
            "SELECT COUNT(*) as cnt FROM channels WHERE slug = ?", (slug,)
        ).fetchone()
        if exists["cnt"] == 0:
            conn.execute(
                "INSERT INTO channels (name, slug, config_json, active) VALUES (?, ?, ?, ?)",
                (name, slug, "{}", 1),
            )
            logger.info("Migration: seeded channel %s (%s)", slug, name)
    
    # Fallback: if channels table is still empty, seed at least one from settings
    row_empty = conn.execute("SELECT COUNT(*) as cnt FROM channels").fetchone()
    if row_empty["cnt"] == 0 and ACTIVE_CHANNELS:
        conn.execute(
            "INSERT INTO channels (name, slug, config_json, active) VALUES (?, ?, ?, ?)",
            (ACTIVE_CHANNELS[0].capitalize(), ACTIVE_CHANNELS[0], "{}", 1),
        )
        conn.execute("UPDATE videos SET channel_id = 1 WHERE channel_id IS NULL")
        logger.warning("Migration: channels table empty — bootstrapped with %s", ACTIVE_CHANNELS[0])

    # Seed yt_studio_url for existing channels (only if not yet set)
    studio_urls = {
        "canal2": "https://studio.youtube.com/channel/UC32VJJKqpbiEExfEHYGxdNw/editing/profile",
        "canal3": "https://studio.youtube.com/channel/UCejkjoNtUs99-LPBEYC7rPQ/editing/profile",
        "canal4": "https://studio.youtube.com/channel/UC9IOZKc0O4mBJ_Vb1x7czPg/editing/profile",
        "canal5": "https://studio.youtube.com/channel/UCDZi5NrlYnncYVlnZ0O7wKA/editing/profile",
    }
    for slug, url in studio_urls.items():
        conn.execute(
            "UPDATE channels SET yt_studio_url = ? WHERE slug = ? AND yt_studio_url IS NULL",
            (url, slug),
        )
    logger.info("Migration: seeded yt_studio_url for existing channels")

    # Check which channels need profile seeding (missing description, banner, or avatar)
    channels_needing_profile: list[str] = []
    all_channels = conn.execute("SELECT slug, description, banner_url, avatar_url FROM channels").fetchall()
    for ch in all_channels:
        if not any([ch["description"], ch["banner_url"], ch["avatar_url"]]):
            channels_needing_profile.append(ch["slug"])
    if channels_needing_profile:
        logger.info("Channels needing profile seeding: %s", channels_needing_profile)

    # ── v6: Monetization + Milestones + Analytics columns & tables ──
    # Add monetization columns to channels (idempotent)
    ch_mon_columns = [
        ("cpm_min", "REAL"),
        ("cpm_max", "REAL"),
        ("monetization_vertical", "TEXT"),
        ("ypp_status", "TEXT DEFAULT 'not_eligible'"),
    ]
    for col_name, col_def in ch_mon_columns:
        if col_name not in existing_ch:
            try:
                conn.execute(f"ALTER TABLE channels ADD COLUMN {col_name} {col_def}")
                logger.info("Migration: added %s column to channels", col_name)
            except sqlite3.OperationalError:
                pass

    # Add yt_studio_url column to channels (idempotent)
    if "yt_studio_url" not in existing_ch:
        try:
            conn.execute("ALTER TABLE channels ADD COLUMN yt_studio_url TEXT")
            logger.info("Migration: added yt_studio_url column to channels")
        except sqlite3.OperationalError:
            pass

    # Add revenue columns to channel_stats_history (idempotent)
    csh_rev_columns = [
        ("estimated_revenue_min", "REAL DEFAULT 0"),
        ("estimated_revenue_max", "REAL DEFAULT 0"),
    ]
    for col_name, col_def in csh_rev_columns:
        if col_name not in existing_csh:
            try:
                conn.execute(f"ALTER TABLE channel_stats_history ADD COLUMN {col_name} {col_def}")
                logger.info("Migration: added %s column to channel_stats_history", col_name)
            except sqlite3.OperationalError:
                pass

    # Add revenue columns to video_stats_history (idempotent)
    existing_vsh = {row[1] for row in conn.execute("PRAGMA table_info(video_stats_history)").fetchall()}
    vsh_rev_columns = [
        ("estimated_revenue_min", "REAL DEFAULT 0"),
        ("estimated_revenue_max", "REAL DEFAULT 0"),
    ]
    for col_name, col_def in vsh_rev_columns:
        if col_name not in existing_vsh:
            try:
                conn.execute(f"ALTER TABLE video_stats_history ADD COLUMN {col_name} {col_def}")
                logger.info("Migration: added %s column to video_stats_history", col_name)
            except sqlite3.OperationalError:
                pass

    # Create channel_milestones table (idempotent)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            metric_type TEXT NOT NULL,
            target_value REAL NOT NULL,
            label TEXT NOT NULL,
            tier TEXT DEFAULT 'standard',
            sort_order INTEGER DEFAULT 0,
            status TEXT DEFAULT 'in_progress',
            achieved_at TEXT,
            UNIQUE(channel_id, metric_type, target_value)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cm_channel ON channel_milestones(channel_id, status)")

    # Create video_analytics_detailed table (idempotent)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_analytics_detailed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            yt_video_id TEXT NOT NULL,
            report_type TEXT NOT NULL,
            dimension TEXT,
            metric_value REAL NOT NULL,
            fetched_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vad_video ON video_analytics_detailed(video_id, report_type)")
    logger.info("Migration: v6 tables ensured (channel_milestones, video_analytics_detailed)")

    # Seed CPM values from channel configs if channels already exist
    cpm_seeds = {
        "canal2": (5.0, 12.0, "Bienestar, Libros, Viajes, Tecnologia, Salud"),
        "canal3": (8.0, 18.0, "Educacion, Viajes, Libros, Tecnologia, Inversion"),
        "canal4": (5.0, 12.0, "Aventura, Viajes, Libros, Educacion, Documentales"),
    }
    for slug, (cpm_min, cpm_max, vertical) in cpm_seeds.items():
        ch = conn.execute(
            "SELECT id, cpm_min, cpm_max FROM channels WHERE slug = ?", (slug,)
        ).fetchone()
        if ch and (ch["cpm_min"] is None or ch["cpm_max"] is None):
            conn.execute(
                "UPDATE channels SET cpm_min=?, cpm_max=?, monetization_vertical=? WHERE slug=?",
                (cpm_min, cpm_max, vertical, slug),
            )
            logger.info("Migration: seeded CPM for %s (%.0f-%.0f USD)", slug, cpm_min, cpm_max)
    conn.commit()

    # ── v4: Normalize media paths to project-root-relative form ──
    normalize_media_paths(conn, logger)

    # ── v9: 3-phase pipeline (generate → upload → publish) ──
    _migrate_v9(conn, logger)

    # ── v10: optimal publish slots (data-driven peak hour calculation) ──
    _migrate_v10(conn, logger)

    # ── v11: media_file_locks (prevents cross-job file deletion race) ──
    _migrate_v11(conn, logger)

    # ── v12: dispatch backoff (failed_attempts + last_failed_at on planned_slots) ──
    _migrate_v12(conn, logger)

    # ── v13: fix optimal_publish_slots CHECK constraint (1-3 → 1-5) ──
    _migrate_v13(conn, logger)
    
    conn.commit()
    conn.close()
    
    # Generate channel profiles (description, banner, avatar) for any channel lacking them
    if channels_needing_profile:
        for slug in channels_needing_profile:
            _seed_channel_profile(slug)

    # Ensure content_schedules table exists (idempotent — adds video_id column if missing)
    _migrate_content_schedules(db_path)


def _migrate_shorts_planning_config(conn, logger):
    """Migrate shorts_planning_config to v4.1 column schema (idempotent).
    
    Adds shorts_native_per_day and shorts_clip_per_day columns,
    migrates data from old shorts_per_day column, and keeps old columns
    (SQLite does not support DROP COLUMN easily).
    """
    # Check if the table exists
    table_exists = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='shorts_planning_config'"
    ).fetchone()[0]
    if not table_exists:
        return
    
    existing = {row[1] for row in conn.execute("PRAGMA table_info(shorts_planning_config)").fetchall()}
    
    # Add new columns if missing
    new_columns = [
        ("shorts_native_per_day", "INTEGER DEFAULT 3"),
        ("shorts_clip_per_day", "INTEGER DEFAULT 2"),
        ("shorts_clips_per_long", "INTEGER DEFAULT 3"),
    ]
    had_old_column = "shorts_per_day" in existing
    columns_added = False
    
    for col_name, col_def in new_columns:
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE shorts_planning_config ADD COLUMN {col_name} {col_def}")
                logger.info("Migration: added %s column to shorts_planning_config", col_name)
                columns_added = True
            except sqlite3.OperationalError:
                pass
    
    # Migrate data from old shorts_per_day if it exists and new columns were just created
    if had_old_column and columns_added:
        # Rough migration: clips = min(old_value, 2), native = max(1, old_value - 2)
        updated = conn.execute("""
            UPDATE shorts_planning_config
            SET shorts_clip_per_day = MIN(shorts_per_day, 2),
                shorts_native_per_day = MAX(1, shorts_per_day - 2)
        """).rowcount
        if updated:
            logger.info(
                "Migration: migrated %d shorts_planning_config rows "
                "(shorts_per_day → native/clip split)", updated
            )
    elif had_old_column:
        # New columns already existed — just migrate data if nulls remain
        updated = conn.execute("""
            UPDATE shorts_planning_config
            SET shorts_clip_per_day = COALESCE(shorts_clip_per_day, MIN(shorts_per_day, 2)),
                shorts_native_per_day = COALESCE(shorts_native_per_day, MAX(1, shorts_per_day - 2))
            WHERE shorts_clip_per_day IS NULL OR shorts_native_per_day IS NULL
        """).rowcount
        if updated:
            logger.info(
                "Migration: backfilled %d shorts_planning_config rows with null new columns",
                updated
            )


def _seed_channel_profile(slug: str):
    """Generate banner, avatar, and description for a channel on first creation."""
    import logging
    _log = logging.getLogger(__name__)

    try:
        from pipeline.channel_profile_generator import generate_channel_profile
        from config.settings import DATABASE_PATH
        import sqlite3

        _log.info("Generating channel profile for %s...", slug)
        profile = generate_channel_profile(slug)

        conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """UPDATE channels
               SET description = ?,
                   banner_url = ?,
                   avatar_url = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE slug = ?""",
            (profile["description"], profile["banner_url"], profile["avatar_url"], slug),
        )
        conn.commit()
        conn.close()

        _log.info(
            "%s profile seeded: desc=%d chars, banner=%s, avatar=%s",
            slug,
            len(profile["description"]),
            profile["banner_url"][-40:] if profile["banner_url"] else "(none)",
            profile["avatar_url"][-40:] if profile["avatar_url"] else "(none)",
        )

    except Exception as exc:
        _log.warning("Failed to seed %s profile: %s", slug, exc)


def _migrate_content_schedules(db_path: str = None):
    """Ensure content_schedules table exists with correct schema (idempotent)."""
    import logging
    _log = logging.getLogger(__name__)
    
    if db_path is None:
        from config.settings import DATABASE_PATH
        db_path = str(DATABASE_PATH)
    
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    
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


def _migrate_v6(conn, logger):
    """Idempotent v6 migration: add viral columns to raw_content and source_mode to planned_slots.

    Uses per-column existence checks (PRAGMA table_info) to avoid "duplicate column" errors.
    """
    import sqlite3

    # ── raw_content viral columns ──────────────────────────────
    existing_rc = {row[1] for row in conn.execute("PRAGMA table_info(raw_content)").fetchall()}
    viral_columns = [
        ("source_mode", "TEXT DEFAULT 'original'"),
        ("viral_original_title", "TEXT"),
        ("viral_original_description", "TEXT"),
        ("viral_original_thumbnail_url", "TEXT"),
        ("viral_original_video_url", "TEXT"),
        ("viral_views", "INTEGER DEFAULT 0"),
        ("viral_upload_date", "TEXT"),
        ("viral_duration_sec", "INTEGER DEFAULT 0"),
        ("viral_channel_name", "TEXT"),
        ("viral_score", "REAL DEFAULT 0.0"),
        ("viral_script_es", "TEXT"),
        ("viral_meta_json", "TEXT"),
    ]
    added = 0
    for col_name, col_def in viral_columns:
        if col_name not in existing_rc:
            try:
                conn.execute(f"ALTER TABLE raw_content ADD COLUMN {col_name} {col_def}")
                added += 1
            except sqlite3.OperationalError as e:
                logger.debug("v6 raw_content.%s: %s", col_name, e)

    # Create indexes for viral queries (idempotent)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_raw_source_mode ON raw_content(source_mode)",
        "CREATE INDEX IF NOT EXISTS idx_raw_viral_score ON raw_content(viral_score)",
    ]:
        try:
            conn.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    if added > 0:
        logger.info("Migration v6: added %d viral columns to raw_content", added)
    else:
        logger.debug("Migration v6: viral columns already present in raw_content")

    # ── planned_slots source_mode ──────────────────────────────
    existing_ps = {row[1] for row in conn.execute("PRAGMA table_info(planned_slots)").fetchall()}
    if "source_mode" not in existing_ps:
        try:
            conn.execute("ALTER TABLE planned_slots ADD COLUMN source_mode TEXT DEFAULT 'original'")
            logger.info("Migration v6: added source_mode column to planned_slots")
        except sqlite3.OperationalError as e:
            logger.debug("v6 planned_slots.source_mode: %s", e)

    # ── shorts_planned_slots source_mode ───────────────────────
    try:
        existing_sps = {row[1] for row in conn.execute("PRAGMA table_info(shorts_planned_slots)").fetchall()}
    except sqlite3.OperationalError:
        existing_sps = set()
    if "source_mode" not in existing_sps:
        try:
            conn.execute("ALTER TABLE shorts_planned_slots ADD COLUMN source_mode TEXT DEFAULT 'original'")
            logger.info("Migration v6: added source_mode column to shorts_planned_slots")
        except sqlite3.OperationalError as e:
            logger.debug("v6 shorts_planned_slots.source_mode: %s", e)

    # ── videos source_url + source_mode (viral original video reference) ──
    existing_vid = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    if "source_url" not in existing_vid:
        try:
            conn.execute("ALTER TABLE videos ADD COLUMN source_url TEXT")
            logger.info("Migration v6: added source_url column to videos")
        except sqlite3.OperationalError as e:
            logger.debug("v6 videos.source_url: %s", e)
    if "source_mode" not in existing_vid:
        try:
            conn.execute("ALTER TABLE videos ADD COLUMN source_mode TEXT DEFAULT 'original'")
            logger.info("Migration v6: added source_mode column to videos")
        except sqlite3.OperationalError as e:
            logger.debug("v6 videos.source_mode: %s", e)
    # Backfill source_mode from source_url for existing records
    conn.execute(
        "UPDATE videos SET source_mode = 'viral' WHERE source_url IS NOT NULL AND source_url != '' AND (source_mode IS NULL OR source_mode = '' OR source_mode = 'original')"
    )
    conn.execute(
        "UPDATE videos SET source_mode = 'original' WHERE source_mode IS NULL OR source_mode = ''"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_source_mode ON videos(source_mode)")

    # ── v11: scheduled_upload_at for randomized upload dispatch ──
    if "scheduled_upload_at" not in existing_vid:
        try:
            conn.execute("ALTER TABLE videos ADD COLUMN scheduled_upload_at TEXT")
            logger.info("Migration v11: added scheduled_upload_at column to videos")
        except sqlite3.OperationalError as e:
            logger.debug("v11 videos.scheduled_upload_at: %s", e)

    conn.commit()


def _migrate_v7(conn, logger):
    """Idempotent v7 migration: channel_daily_watchtime table for 365-day watch time tracking.

    Stores daily estimatedMinutesWatched and subscribersGained from YouTube Analytics API
    to compute cumulative watch hours for YPP monetization progress.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_daily_watchtime (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            estimated_minutes_watched REAL DEFAULT 0.0,
            subscribers_gained INTEGER DEFAULT 0,
            UNIQUE(channel_id, date)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cdw_channel_date
        ON channel_daily_watchtime(channel_id, date)
    """)
    conn.commit()
    logger.info("Migration v7: channel_daily_watchtime table ensured")


def _migrate_v9(conn, logger):
    """Idempotent v9 migration: 3-phase pipeline (generate → upload → publish).
    
    Adds:
      - planned_slots.target_public_at (peak publish time, separate from target_upload_at which now = upload window time)
      - planned_slots.upload_window_start / upload_window_end (per-slot upload window, denormalized for queries)
      - Channel config_json defaults: GENERATION_LEAD_HOURS, UPLOAD_WINDOW_START, UPLOAD_WINDOW_END
    """
    # ── planned_slots: target_public_at ──────────────────────────
    existing_ps = {row[1] for row in conn.execute("PRAGMA table_info(planned_slots)").fetchall()}
    
    ps_columns = [
        ("target_public_at", "TIMESTAMP"),
        ("upload_window_start", "INTEGER DEFAULT 9"),  # hour (0-23)
        ("upload_window_end", "INTEGER DEFAULT 11"),   # hour (0-23)
    ]
    added = 0
    for col_name, col_def in ps_columns:
        if col_name not in existing_ps:
            try:
                conn.execute(f"ALTER TABLE planned_slots ADD COLUMN {col_name} {col_def}")
                added += 1
                logger.info("Migration v9: added %s column to planned_slots", col_name)
            except Exception as e:
                logger.debug("v9 planned_slots.%s: %s", col_name, e)
    
    if added > 0:
        # Backfill target_public_at from target_upload_at for existing pending slots
        conn.execute("""
            UPDATE planned_slots 
            SET target_public_at = target_upload_at 
            WHERE target_public_at IS NULL AND target_upload_at IS NOT NULL
        """)
        logger.info("Migration v9: backfilled target_public_at from target_upload_at")
    
    # ── Seed channel config_json defaults for existing active channels ──
    channels = conn.execute("SELECT id, slug, config_json FROM channels WHERE active = 1").fetchall()
    seeded = 0
    for ch in channels:
        try:
            config = json.loads(ch["config_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            config = {}
        
        modified = False
        defaults = {
            "GENERATION_LEAD_HOURS": 36,
            "UPLOAD_WINDOW_START": 9,
            "UPLOAD_WINDOW_END": 11,
        }
        for key, val in defaults.items():
            if key not in config:
                config[key] = val
                modified = True
        
        if modified:
            conn.execute(
                "UPDATE channels SET config_json = ? WHERE id = ?",
                (json.dumps(config, ensure_ascii=False), ch["id"]),
            )
            seeded += 1
    
    if seeded > 0:
        logger.info("Migration v9: seeded 3-phase config defaults for %d channels", seeded)

    # ── Create index for pending slot queries by target_public_at ──
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ps_target_public ON planned_slots(target_public_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ps_window ON planned_slots(upload_window_start, upload_window_end)"
    )
    conn.commit()
    logger.info("Migration v9: complete (added %d columns, seeded %d channels)", added, seeded)


def _migrate_v12(conn, logger):
    """Idempotent v12 migration: dispatch backoff columns on planned_slots.

    Adds failed_attempts and last_failed_at to prevent infinite retry loops
    when a slot dispatch fails due to transient conditions (low RAM, concurrency).
    Slots that fail repeatedly get exponential cooldown to avoid hundreds of
    wasted attempts per day.

    Also adds FIRST_COMMENT_ENABLED config default to settings.
    """
    existing_ps = {row[1] for row in conn.execute("PRAGMA table_info(planned_slots)").fetchall()}
    ps_columns = [
        ("failed_attempts", "INTEGER DEFAULT 0"),
        ("last_failed_at", "TIMESTAMP"),
    ]
    for col_name, col_def in ps_columns:
        if col_name not in existing_ps:
            try:
                conn.execute(f"ALTER TABLE planned_slots ADD COLUMN {col_name} {col_def}")
                logger.info("Migration v12: added %s column to planned_slots", col_name)
            except Exception as e:
                logger.debug("v12 planned_slots.%s: %s", col_name, e)
    conn.commit()
    logger.info("Migration v12: complete")


def _migrate_v13(conn, logger):
    """Idempotent v13 migration: fix optimal_publish_slots CHECK constraint.

    The original v10 migration code had CHECK(slot_rank BETWEEN 1 AND 5)
    but some databases were created with CHECK(slot_rank BETWEEN 1 AND 3),
    causing the optimal slots calculator to fail when generating 4 peak slots
    (NUM_PEAKS_SHORT was 4).

    This migration recreates the table with the correct 1-5 range if the
    constraint is currently 1-3. Uses table recreation since SQLite doesn't
    support ALTER TABLE for CHECK constraints.
    """
    # Check current constraint
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='optimal_publish_slots'"
    ).fetchone()

    if not row:
        logger.debug("Migration v13: optimal_publish_slots doesn't exist yet — skipping")
        return

    ddl = row[0] or ""
    if "BETWEEN 1 AND 5" in ddl:
        logger.debug("Migration v13: constraint already 1-5 — nothing to do")
        return

    logger.info("Migration v13: fixing optimal_publish_slots CHECK constraint (1-3 → 1-5)")

    # Step 1: create new table with correct constraint
    conn.execute("""
        CREATE TABLE optimal_publish_slots_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            content_type        TEXT NOT NULL DEFAULT 'long',
            slot_rank           INTEGER NOT NULL CHECK(slot_rank BETWEEN 1 AND 5),
            target_hour         INTEGER NOT NULL,
            target_minute       INTEGER NOT NULL DEFAULT 0,
            timezone            TEXT NOT NULL,
            score               REAL DEFAULT 0.0,
            confidence          REAL DEFAULT 0.0,
            audience_focus      TEXT DEFAULT 'blend',
            metrics_snapshot    TEXT DEFAULT '{}',
            data_sources        TEXT DEFAULT '{}',
            audience_split      TEXT DEFAULT '{}',
            calculated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used_count          INTEGER DEFAULT 0,
            total_views_result  INTEGER DEFAULT 0,
            avg_views_result    REAL DEFAULT 0.0,
            UNIQUE(channel_id, content_type, slot_rank)
        )
    """)

    # Step 2: copy data
    conn.execute(
        "INSERT INTO optimal_publish_slots_new "
        "SELECT * FROM optimal_publish_slots"
    )

    # Step 3: drop old table
    conn.execute("DROP TABLE optimal_publish_slots")

    # Step 4: rename
    conn.execute(
        "ALTER TABLE optimal_publish_slots_new "
        "RENAME TO optimal_publish_slots"
    )

    # Step 5: recreate indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ops_channel "
        "ON optimal_publish_slots(channel_id, content_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ops_calculated "
        "ON optimal_publish_slots(calculated_at)"
    )

    conn.commit()
    logger.info("Migration v13: optimal_publish_slots constraint fixed (1-5)")


def _migrate_v10(conn, logger):
    """Idempotent v10 migration: optimal_publish_slots for data-driven peak hour calculation.

    Stores 3 optimal publish slots per channel per content type (long / short),
    calculated daily from YouTube Analytics viewer activity by hour + historical
    video performance data. Supports Spain + LATAM audience split detection.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS optimal_publish_slots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
            content_type        TEXT NOT NULL DEFAULT 'long',
            slot_rank           INTEGER NOT NULL CHECK(slot_rank BETWEEN 1 AND 5),
            target_hour         INTEGER NOT NULL,
            target_minute       INTEGER NOT NULL DEFAULT 0,
            timezone            TEXT NOT NULL,
            score               REAL DEFAULT 0.0,
            confidence          REAL DEFAULT 0.0,
            audience_focus      TEXT DEFAULT 'blend',
            metrics_snapshot    TEXT DEFAULT '{}',
            data_sources        TEXT DEFAULT '{}',
            audience_split      TEXT DEFAULT '{}',
            calculated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used_count          INTEGER DEFAULT 0,
            total_views_result  INTEGER DEFAULT 0,
            avg_views_result    REAL DEFAULT 0.0,
            UNIQUE(channel_id, content_type, slot_rank)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ops_channel "
        "ON optimal_publish_slots(channel_id, content_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ops_calculated "
        "ON optimal_publish_slots(calculated_at)"
    )
    conn.commit()
    logger.info("Migration v10: optimal_publish_slots table ensured")


def _migrate_v11(conn, logger):
    """Idempotent v11 migration: media_file_locks table.

    Tracks which files are owned by which active generation jobs,
    so the pipeline cleanup never deletes files that another job
    is still using (race condition fix for black-screen bug).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_file_locks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER NOT NULL REFERENCES generation_jobs(id) ON DELETE CASCADE,
            file_path   TEXT NOT NULL,
            locked_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(job_id, file_path)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mfl_job "
        "ON media_file_locks(job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mfl_path "
        "ON media_file_locks(file_path)"
    )
    conn.commit()
    logger.info("Migration v11: media_file_locks table ensured")


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
            conn.execute("DELETE FROM planned_slots WHERE channel_id = ?", (channel_id,))
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
    
    def get_videos(self, channel_id: int = None, status: str = None, limit: int = 50,
                    offset: int = 0, playlist_id: int = None,
                    source_mode: str = None) -> list[dict]:
        with self._connect() as conn:
            q = ("SELECT v.*, c.name as channel_name, "
                 "yp.name as target_playlist_name "
                 "FROM videos v "
                 "LEFT JOIN channels c ON v.channel_id = c.id "
                 "LEFT JOIN youtube_playlists yp ON v.target_playlist_id = yp.id "
                 "WHERE 1=1")
            params = []
            if channel_id is not None:
                q += " AND v.channel_id = ?"
                params.append(channel_id)
            if playlist_id is not None:
                q += " AND v.target_playlist_id = ?"
                params.append(playlist_id)
            if source_mode:
                q += " AND v.source_mode = ?"
                params.append(source_mode)
            if status:
                q += " AND v.status = ?"
                params.append(status)
            else:
                # Exclude videos deleted from YouTube (soft-delete) and
                # failed/error videos by default to keep listings clean.
                # Pass status='error' explicitly to see failures.
                q += " AND v.status NOT IN ('deleted_on_yt', 'error')"
            q += " ORDER BY v.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    
    def get_video(self, video_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT v.*, c.name as channel_name, "
                "yp.name as target_playlist_name "
                "FROM videos v "
                "LEFT JOIN channels c ON v.channel_id = c.id "
                "LEFT JOIN youtube_playlists yp ON v.target_playlist_id = yp.id "
                "WHERE v.id = ?", (video_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_videos_published_today(self, channel_id: int) -> int:
        """Count videos successfully uploaded/published today for a channel.

        Used by the recovery planner to determine how many of today's
        target videos have already been published.

        Returns count of videos where:
        - channel_id matches
        - uploaded_at is today (local time)
        - yt_video_id is not null (successfully uploaded to YouTube)
        - status is one of: uploaded, uploaded_private, published
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM videos
                   WHERE channel_id = ?
                     AND DATE(uploaded_at) = DATE('now', 'localtime')
                     AND yt_video_id IS NOT NULL
                     AND status IN ('uploaded', 'uploaded_private', 'published')""",
                (channel_id,),
            ).fetchone()
        return row["cnt"] if row else 0
    
    def update_video(self, video_id: int, **kwargs) -> bool:
        allowed = ["titulo_final", "description", "tags_json", "title_options",
                    "privacy_status", "status", "progress", "progress_phase",
                    "video_path", "thumbnail_path", "audio_path", "duracion_seg",
                    "script_id", "channel_id", "checkpoint_data", "timing_data",
                    "source_url", "source_mode",
                    # ── Scheduled publishing ──
                    "publish_mode", "target_public_at", "published_at",
                    "peak_source", "auto_playlist_id", "auto_playlist_name",
                    "target_playlist_id", "target_playlist_slug",
                    "manual_altered_content_done", "manual_end_screens_done",
                    "generation_started_at", "generation_finished_at",
                    "scheduled_upload_at",
                    # ── Cache-busting for frontend ──
                    "updated_at"]
        
        # ── Guard: never overwrite status to 'error' if video was already uploaded ──
        # A video with a YouTube ID was successfully published. Pipeline failures
        # (zombie threads, orphan detector races) must not overwrite that fact.
        if kwargs.get("status") == "error":
            existing = self.get_video(video_id)
            if existing and existing.get("yt_video_id"):
                logger.warning(
                    "Rejecting status='error' for video #%d: "
                    "already uploaded to YouTube (yt_video_id=%s)",
                    video_id, existing["yt_video_id"])
                kwargs = {k: v for k, v in kwargs.items() if k != "status"}
        
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
    
    def mark_video_uploaded(self, video_id, yt_video_id, yt_url, status: str = 'uploaded'):
        """Mark a video as uploaded to YouTube. Supports scheduled mode statuses."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE videos SET yt_video_id=?, yt_url=?, uploaded_at=CURRENT_TIMESTAMP, status=? WHERE id=?",
                (yt_video_id, yt_url, status, video_id),
            )
            conn.commit()
    
    def delete_video(self, video_id: int) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM video_scenes WHERE video_id = ?", (video_id,))
            conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            conn.commit()
        return True

    def cleanup_error_videos(self, channel_id: int, older_than_days: int = 7) -> int:
        """Delete videos with status='error' older than X days.
        
        ON DELETE CASCADE handles video_scenes, video_stats_history, etc.
        Returns count of deleted rows.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """DELETE FROM videos
                   WHERE channel_id = ?
                     AND status = 'error'
                     AND created_at < datetime('now', '-' || ? || ' days')""",
                (channel_id, older_than_days),
            )
            conn.commit()
            return cursor.rowcount

    def count_error_videos(self, channel_id: int, older_than_days: int = 7) -> int:
        """Count videos with status='error' older than X days (for dry-run preview)."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM videos
                   WHERE channel_id = ?
                     AND status = 'error'
                     AND created_at < datetime('now', '-' || ? || ' days')""",
                (channel_id, older_than_days),
            ).fetchone()
            return row["cnt"] if row else 0
    
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
        allowed = ["status", "progress", "phase", "error_msg", "video_id",
                   "pipeline_phase", "last_heartbeat_at", "retry_count", "worker_pid",
                   "started_at"]
        fields, values = [], []
        for k, v in kwargs.items():
            if k in allowed and v is not None:
                fields.append(f"{k} = ?")
                values.append(v)
        if kwargs.get("status") == "running":
            if "started_at" not in kwargs:
                fields.append("started_at = COALESCE(started_at, CURRENT_TIMESTAMP)")
            # Reset finished_at when job is (re)started — prevents inconsistent
            # state where finished_at is set but status is still 'running'
            fields.append("finished_at = NULL")
        if kwargs.get("status") in ("completed", "failed"):
            fields.append("finished_at = CURRENT_TIMESTAMP")
        # Clear stale error_msg when job succeeds (fixes ghost orphan errors)
        if kwargs.get("status") == "completed":
            if "error_msg" not in kwargs:
                fields.append("error_msg = NULL")
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
        """Return jobs with status 'queued' or 'running', excluding stale zombies.
        
        Safety filter: running jobs without a heartbeat for >60 min (or never emitted
        one and started >60 min ago) are excluded to prevent zombie progress bars
        from appearing in the UI before the orphan detector cleans them up.
        Queued jobs are always returned (they haven't started yet).
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM generation_jobs 
                WHERE status = 'queued'
                UNION ALL
                SELECT * FROM generation_jobs 
                WHERE status = 'running'
                  AND (
                      -- Heartbeat mode: last heartbeat within 60 min → still alive
                      (last_heartbeat_at IS NOT NULL 
                       AND (julianday('now') - julianday(last_heartbeat_at)) * 1440 <= 60)
                      OR
                      -- No heartbeat yet: only include if started <60 min ago
                      (last_heartbeat_at IS NULL 
                       AND started_at IS NOT NULL
                       AND (julianday('now') - julianday(started_at)) * 1440 <= 60)
                  )
                ORDER BY created_at DESC
            """).fetchall()
        return [dict(r) for r in rows]
    
    def update_heartbeat(self, job_id: int) -> None:
        """Update the last_heartbeat_at timestamp for a running job.
        
        Called every ~30s by the video render thread to signal the
        orphan detector that the render is still alive.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE generation_jobs SET last_heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )
            conn.commit()
    
    def get_next_queued_job(self) -> Optional[dict]:
        """Return the oldest queued job (FIFO), or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    
    def increment_retry(self, job_id: int) -> int:
        """Increment retry_count and return the new value."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE generation_jobs SET retry_count = retry_count + 1 WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT retry_count FROM generation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return row["retry_count"] if row else 0
    
    def update_job_requeue(self, job_id: int, error_msg: str = None) -> None:
        """Reset a failed job to 'queued' for retry, preserving the error message."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE generation_jobs SET status='queued', finished_at=NULL, "
                "started_at=NULL, error_msg=? WHERE id=?",
                (error_msg, job_id),
            )
            conn.commit()
    
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
                canal_name = canal["slug"] if canal else None
                if canal_name:
                    rows = conn.execute(
                        "SELECT * FROM pipeline_log WHERE canal = ? ORDER BY created_at DESC LIMIT ?",
                        (canal_name, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM pipeline_log ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pipeline_log ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
    
    def log_pipeline_event(self, canal: str, phase: str, status: str, message: str = None, 
                           content_id: int = None, duration_ms: int = None):
        """Insert a row into pipeline_log (used by orphan detector and other utilities)."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO pipeline_log (canal, phase, status, message, content_id, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (canal, phase, status, message, content_id, duration_ms),
            )
            conn.commit()
    
    # ── Orphan Detection ─────────────────────────────────────
    
    # Heartbeat-based orphan detection: a job is declared dead only if its last
    # heartbeat was >N min ago (configurable via env).  Defaults have been raised
    # to 120 min (2h) for all phases to prevent false-positive orphan detection
    # during long renders (50+ scene segments can take 40-90 min with MoviePy).
    HEARTBEAT_ORPHAN_TIMEOUT_MINUTES = int(
        __import__("os").getenv("HEARTBEAT_ORPHAN_TIMEOUT_MIN", "60")
    )
    NONVIDEO_HEARTBEAT_ORPHAN_MIN = int(
        __import__("os").getenv("NONVIDEO_HEARTBEAT_ORPHAN_MIN", "30")
    )
    
    # Legacy fallback: jobs without heartbeat support use started_at-based timeout
    VIDEO_PHASE_TIMEOUT_MINUTES = 120  # 2h — max ceiling for renders that never emitted heartbeat
    DEFAULT_ORPHAN_TIMEOUT_MINUTES = 60  # 1h — general fallback timeout

    def cleanup_orphaned_jobs(self, timeout_minutes: int = None) -> dict:
        """Detect and clean up orphaned generation jobs and videos.
        
        Three types of orphans:
        1. Jobs stuck in 'running' with no finished_at for > timeout_minutes
           (video phase: 20 min; all others: 60 min).
        2. Videos stuck in 'generating' with no active job for > timeout_minutes
        3. Videos in 'error' with job still 'running' (zombie-thread race)
        
        Returns a dict with counts of cleaned items.
        """
        import logging
        logger = logging.getLogger("autotube.orphans")
        
        if timeout_minutes is None:
            default_timeout = self.DEFAULT_ORPHAN_TIMEOUT_MINUTES
        else:
            default_timeout = timeout_minutes
        
        result = {"jobs_failed": 0, "videos_reset": 0, "details": []}
        
        with self._connect() as conn:
            # ── Type 1: Jobs stuck in 'running' beyond timeout ──
            # Separate queries for video phase vs other phases.
            # Video phase uses heartbeat when available (>20 min w/o heartbeat = dead).
            # Fallback to started_at for jobs that never emitted a heartbeat.
            
            # Type 1a: Video-phase jobs — heartbeat-aware orphan detection
            # A video job is orphaned if:
            #   (a) It has heartbeats but the last one was >20 min ago, OR
            #   (b) It has NEVER emitted a heartbeat AND started >480 min ago (legacy fallback)
            orphan_video_jobs = conn.execute("""
                SELECT j.id as job_id, j.video_id, j.channel_id, j.phase, j.started_at,
                       j.last_heartbeat_at, j.worker_pid,
                       cast((julianday('now') - julianday(j.started_at)) * 86400 as integer) as elapsed_sec,
                       c.slug as channel_slug, v.status as video_status
                  FROM generation_jobs j
                  JOIN channels c ON j.channel_id = c.id
                  LEFT JOIN videos v ON j.video_id = v.id
                  WHERE j.status = 'running'
                    AND j.finished_at IS NULL
                    AND j.started_at IS NOT NULL
                    AND j.phase = 'video'
                     AND (
                         -- Heartbeat mode: last heartbeat > configured timeout → truly dead
                         (j.last_heartbeat_at IS NOT NULL
                          AND (julianday('now') - julianday(j.last_heartbeat_at)) * 1440 > ?)
                         OR
                         -- Legacy fallback: no heartbeat ever + started >VIDEO_PHASE_TIMEOUT min ago
                         (j.last_heartbeat_at IS NULL
                          AND (julianday('now') - julianday(j.started_at)) * 1440 > ?)
                     )
            """, (self.HEARTBEAT_ORPHAN_TIMEOUT_MINUTES, self.VIDEO_PHASE_TIMEOUT_MINUTES,)).fetchall()
            
            # Type 1b: Non-video-phase jobs — heartbeat-aware when available,
            # fallback to generous started_at timeout when no heartbeats exist.
            # Upload phase can emit heartbeats via the resumable upload callback;
            # other phases (metadata, thumbnail) are short-lived and use the
            # job-level started_at timeout.
            orphan_jobs = conn.execute("""
                SELECT j.id as job_id, j.video_id, j.channel_id, j.phase, j.started_at,
                       j.last_heartbeat_at, j.worker_pid,
                       cast((julianday('now') - julianday(j.started_at)) * 86400 as integer) as elapsed_sec,
                       c.slug as channel_slug, v.status as video_status
                  FROM generation_jobs j
                  JOIN channels c ON j.channel_id = c.id
                  LEFT JOIN videos v ON j.video_id = v.id
                  WHERE j.status = 'running'
                    AND j.finished_at IS NULL
                    AND j.started_at IS NOT NULL
                     AND j.phase != 'video'
                     AND (
                         -- Heartbeat mode: last heartbeat > N min ago → truly dead
                         (j.last_heartbeat_at IS NOT NULL
                          AND (julianday('now') - julianday(j.last_heartbeat_at)) * 1440 > ?)
                         OR
                          -- Legacy fallback: no heartbeat ever + started >120 min (2h) ago
                          -- 2h is enough for upload + metadata after a long render
                          (j.last_heartbeat_at IS NULL
                           AND (julianday('now') - julianday(j.started_at)) * 1440 > 120)
                     )
            """, (self.NONVIDEO_HEARTBEAT_ORPHAN_MIN,)).fetchall()
            
            # Combine video-phase and non-video orphans for processing
            all_orphan_jobs = list(orphan_video_jobs) + list(orphan_jobs)
            
            for row in all_orphan_jobs:
                r = dict(row)
                logger.warning(
                    "Orphan job #%d (channel=%s, video=%s, phase=%s, elapsed=%ds, pid=%s): marking as failed",
                    r["job_id"], r["channel_slug"], r["video_id"], r["phase"], r["elapsed_sec"],
                    r.get("worker_pid", "?"),
                )
                
                # Mark job as failed
                conn.execute(
                    "UPDATE generation_jobs SET status='failed', error_msg=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f"Orphaned: process lost after {r['elapsed_sec']}s", r["job_id"]),
                )
                
                # ── Kill the orphaned worker process if we have its PID ──
                worker_pid = r.get("worker_pid")
                if worker_pid:
                    try:
                        import os as _os
                        import signal as _signal
                        logger.warning(
                            "Orphan job #%d: sending SIGTERM to worker PID %d",
                            r["job_id"], worker_pid,
                        )
                        _os.kill(worker_pid, _signal.SIGTERM)
                        # Give it 5 seconds to die gracefully, then SIGKILL
                        import time as _time
                        _time.sleep(2)
                        try:
                            _os.kill(worker_pid, 0)  # check if still alive
                            logger.warning(
                                "Orphan job #%d: worker PID %d still alive after SIGTERM — sending SIGKILL",
                                r["job_id"], worker_pid,
                            )
                            _os.kill(worker_pid, _signal.SIGKILL)
                        except OSError:
                            pass  # process already dead
                    except OSError as exc:
                        logger.debug(
                            "Orphan job #%d: could not kill PID %d: %s",
                            r["job_id"], worker_pid, exc,
                        )
                
                # Mark associated video as error if it's still 'generating'
                if r["video_id"] and r["video_status"] == "generating":
                    # Guard: never overwrite status if video was already uploaded (zombie-thread race)
                    yt_check = conn.execute(
                        "SELECT yt_video_id FROM videos WHERE id=?", (r["video_id"],)
                    ).fetchone()
                    if yt_check and yt_check[0]:
                        logger.warning(
                            "Skipping orphan error for video #%d: already uploaded (yt=%s)",
                            r["video_id"], yt_check[0])
                    else:
                        conn.execute(
                            "UPDATE videos SET status='error', progress_phase='orphaned' WHERE id=?",
                            (r["video_id"],),
                        )
                        result["videos_reset"] += 1
                
                # Log to pipeline_log
                conn.execute(
                    """INSERT INTO pipeline_log (canal, phase, status, message)
                       VALUES (?, 'orphan_detector', 'error', ?)""",
                    (r["channel_slug"], f"Job #{r['job_id']} orphaned after {r['elapsed_sec']}s (phase={r['phase']})"),
                )
                
                result["jobs_failed"] += 1
                result["details"].append({
                    "type": "orphan_job",
                    "job_id": r["job_id"],
                    "video_id": r["video_id"],
                    "channel": r["channel_slug"],
                    "elapsed_sec": r["elapsed_sec"],
                    "phase": r["phase"],
                })
            
            # ── Type 1b: Jobs with inconsistent state (running + finished_at set) ──
            # These happen when update_job() set finished_at on a 'failed' transition
            # but status was later reverted to 'running' without clearing finished_at.
            inconsistent_jobs = conn.execute("""
                SELECT j.id as job_id, j.video_id, j.channel_id, j.phase, j.started_at, j.finished_at,
                       j.error_msg,
                       cast((julianday('now') - julianday(j.started_at)) * 86400 as integer) as elapsed_sec,
                       c.slug as channel_slug, v.status as video_status
                FROM generation_jobs j
                JOIN channels c ON j.channel_id = c.id
                LEFT JOIN videos v ON j.video_id = v.id
                WHERE j.status = 'running'
                  AND j.finished_at IS NOT NULL
                   AND (julianday('now') - julianday(j.finished_at)) * 1440 > ?
            """, (default_timeout,)).fetchall()
            
            for row in inconsistent_jobs:
                r = dict(row)
                logger.warning(
                    "Inconsistent job #%d (channel=%s, video=%s, phase=%s, elapsed_since_finish=%ds): "
                    "status=running but finished_at is set — marking as failed",
                    r["job_id"], r["channel_slug"], r["video_id"], r["phase"], r["elapsed_sec"]
                )
                
                # Mark job as failed (status was wrong)
                conn.execute(
                    "UPDATE generation_jobs SET status='failed', error_msg=COALESCE(error_msg, 'Inconsistent state: running with finished_at') WHERE id=?",
                    (r["job_id"],),
                )
                
                # Mark associated video as error if it's still 'generating'
                if r["video_id"] and r["video_status"] == "generating":
                    # Guard: never overwrite status if video was already uploaded
                    yt_check = conn.execute(
                        "SELECT yt_video_id FROM videos WHERE id=?", (r["video_id"],)
                    ).fetchone()
                    if yt_check and yt_check[0]:
                        logger.warning(
                            "Skipping inconsistent error for video #%d: already uploaded (yt=%s)",
                            r["video_id"], yt_check[0])
                    else:
                        conn.execute(
                            "UPDATE videos SET status='error', progress_phase='orphaned' WHERE id=?",
                            (r["video_id"],),
                        )
                        result["videos_reset"] += 1
                
                # Log to pipeline_log
                conn.execute(
                    """INSERT INTO pipeline_log (canal, phase, status, message)
                       VALUES (?, 'orphan_detector', 'error', ?)""",
                    (r["channel_slug"],
                     f"Inconsistent job #{r['job_id']}: running with finished_at set ({r['elapsed_sec']}s since finish, error={r['error_msg'][:80] if r['error_msg'] else 'unknown'})"),
                )
                
                result["jobs_failed"] += 1
                result["details"].append({
                    "type": "inconsistent_job",
                    "job_id": r["job_id"],
                    "video_id": r["video_id"],
                    "channel": r["channel_slug"],
                    "elapsed_sec": r["elapsed_sec"],
                    "phase": r["phase"],
                    "error_msg": r["error_msg"][:200] if r["error_msg"] else None,
                })
            
            # ── Type 2: Videos in 'generating' with no active job ──
            orphan_videos = conn.execute("""
                SELECT v.id as video_id, v.channel_id, v.created_at,
                       cast((julianday('now') - julianday(v.created_at)) * 86400 as integer) as elapsed_sec,
                       c.slug as channel_slug
                FROM videos v
                JOIN channels c ON v.channel_id = c.id
                WHERE v.status = 'generating'
                  AND NOT EXISTS (
                    SELECT 1 FROM generation_jobs j
                    WHERE j.video_id = v.id AND j.status IN ('queued', 'running')
                  )
                   AND (julianday('now') - julianday(v.created_at)) * 1440 > ?
            """, (default_timeout,)).fetchall()
            
            for row in orphan_videos:
                r = dict(row)
                logger.warning(
                    "Orphan video #%d (channel=%s, elapsed=%ds): no active job — marking as error",
                    r["video_id"], r["channel_slug"], r["elapsed_sec"]
                )
                
                # Guard: never overwrite status if video was already uploaded
                yt_check = conn.execute(
                    "SELECT yt_video_id FROM videos WHERE id=?", (r["video_id"],)
                ).fetchone()
                if yt_check and yt_check[0]:
                    logger.warning(
                        "Skipping orphan error for video #%d: already uploaded (yt=%s)",
                        r["video_id"], yt_check[0])
                else:
                    conn.execute(
                        "UPDATE videos SET status='error', progress_phase='orphaned' WHERE id=?",
                        (r["video_id"],),
                    )
                    result["videos_reset"] += 1
                
                conn.execute(
                    """INSERT INTO pipeline_log (canal, phase, status, message)
                       VALUES (?, 'orphan_detector', 'error', ?)""",
                    (r["channel_slug"], f"Video #{r['video_id']} orphaned after {r['elapsed_sec']}s (no active job)"),
                )
                
                result["videos_reset"] += 1
                result["details"].append({
                    "type": "orphan_video",
                    "video_id": r["video_id"],
                    "channel": r["channel_slug"],
                    "elapsed_sec": r["elapsed_sec"],
                })
            
            # ── Type 3: Videos in 'error' with job still 'running' ──
            # This catches the race condition where a zombie pipeline thread
            # overwrites the job's "failed" status back to "running" after the
            # error handler already marked the video as "error".
            zombie_led_jobs = conn.execute("""
                SELECT j.id as job_id, j.video_id, j.channel_id, j.phase,
                       j.started_at, j.progress,
                       cast((julianday('now') - julianday(j.started_at)) * 86400 as integer) as elapsed_sec,
                       c.slug as channel_slug, v.status as video_status,
                       v.progress_phase as video_progress_phase
                FROM generation_jobs j
                JOIN channels c ON j.channel_id = c.id
                JOIN videos v ON j.video_id = v.id
                WHERE j.status = 'running'
                  AND v.status = 'error'
            """).fetchall()
            
            for row in zombie_led_jobs:
                r = dict(row)
                logger.warning(
                    "Zombie-thread job #%d (channel=%s, video=#%d, phase=%s, "
                    "elapsed=%ds): video is 'error' but job is still 'running' — marking job as failed",
                    r["job_id"], r["channel_slug"], r["video_id"],
                    r["phase"], r["elapsed_sec"]
                )
                
                conn.execute(
                    "UPDATE generation_jobs SET status='failed', error_msg=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f"Video failed: associated video #{r['video_id']} is in 'error' state "
                     f"(phase={r['video_progress_phase']}, elapsed={r['elapsed_sec']}s)", r["job_id"]),
                )
                
                conn.execute(
                    """INSERT INTO pipeline_log (canal, phase, status, message)
                       VALUES (?, 'orphan_detector', 'error', ?)""",
                    (r["channel_slug"],
                     f"Zombie-thread job #{r['job_id']} resolved: video #{r['video_id']} "
                     f"was already 'error' ({r['video_progress_phase']}) after {r['elapsed_sec']}s"),
                )
                
                result["jobs_failed"] += 1
                result["details"].append({
                    "type": "zombie_thread_job",
                    "job_id": r["job_id"],
                    "video_id": r["video_id"],
                    "channel": r["channel_slug"],
                    "elapsed_sec": r["elapsed_sec"],
                    "video_progress_phase": r["video_progress_phase"],
                })
            
            conn.commit()
        
        if result["jobs_failed"] > 0 or result["videos_reset"] > 0:
            logger.info(
                "Orphan cleanup complete: %d jobs failed, %d videos reset",
                result["jobs_failed"], result["videos_reset"]
            )
        
        return result

    # ── Video Stats History ────────────────────────────────────

    def insert_video_stats(self, video_id: int, yt_video_id: str, stats: dict) -> int | None:
        """Insert a snapshot of YouTube video statistics."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO video_stats_history
                   (video_id, yt_video_id, views, likes, comments,
                    estimated_minutes_watched, average_view_duration,
                    subscribers_gained, estimated_revenue_min, estimated_revenue_max,
                    embeddable)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    video_id,
                    yt_video_id,
                    int(stats.get("viewCount", 0)),
                    int(stats.get("likeCount", 0)),
                    int(stats.get("commentCount", 0)),
                    float(stats.get("estimatedMinutesWatched", 0)),
                    float(stats.get("averageViewDuration", 0)),
                    int(stats.get("subscribersGained", 0)),
                    float(stats.get("estimated_revenue_min", 0)),
                    float(stats.get("estimated_revenue_max", 0)),
                    1 if stats.get("embeddable", True) else 0,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def insert_short_stats(self, short_id: int, yt_video_id: str, stats: dict) -> int | None:
        """Insert a snapshot of YouTube statistics for a short."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO short_stats
                   (short_id, yt_video_id, views, likes, comments,
                    estimated_minutes_watched, average_view_duration,
                    embeddable)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    short_id,
                    yt_video_id,
                    int(stats.get("viewCount", 0)),
                    int(stats.get("likeCount", 0)),
                    int(stats.get("commentCount", 0)),
                    float(stats.get("estimatedMinutesWatched", 0)),
                    float(stats.get("averageViewDuration", 0)),
                    1 if stats.get("embeddable", True) else 0,
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

    # ── Bulk Analytics Updates ─────────────────────────────────

    def batch_update_video_analytics(
        self, video_id_map: dict[str, int], analytics_data: dict[str, dict]
    ) -> int:
        """Update estimated_minutes_watched, average_view_duration, subscribers_gained
        for multiple videos from a single Analytics API call.

        Args:
            video_id_map: Dict mapping yt_video_id → internal video id.
            analytics_data: Dict mapping yt_video_id → {estimatedMinutesWatched,
                            averageViewDuration, subscribersGained}

        Returns:
            Number of rows updated.
        """
        if not analytics_data or not video_id_map:
            return 0

        with self._connect() as conn:
            count = 0
            for yt_id, aid in video_id_map.items():
                data = analytics_data.get(yt_id)
                if not data:
                    continue
                conn.execute(
                    """UPDATE video_stats_history
                       SET estimated_minutes_watched = ?,
                           average_view_duration = ?,
                           subscribers_gained = ?
                       WHERE id = (
                           SELECT MAX(id) FROM video_stats_history
                           WHERE video_id = ? AND yt_video_id = ?
                       )""",
                    (
                        float(data.get("estimatedMinutesWatched", 0)),
                        float(data.get("averageViewDuration", 0)),
                        int(float(data.get("subscribersGained", 0))),
                        aid,
                        yt_id,
                    ),
                )
                count += conn.total_changes
            conn.commit()
        return count

    def batch_update_short_analytics(
        self, short_id_map: dict[str, int], analytics_data: dict[str, dict]
    ) -> int:
        """Update estimated_minutes_watched, average_view_duration for shorts
        from bulk analytics data.

        Args:
            short_id_map: Dict mapping youtube_id → internal short id.
            analytics_data: Dict mapping yt_video_id → {estimatedMinutesWatched,
                            averageViewDuration, subscribersGained}

        Returns:
            Number of rows updated.
        """
        if not analytics_data or not short_id_map:
            return 0

        with self._connect() as conn:
            count = 0
            for yt_id, sid in short_id_map.items():
                data = analytics_data.get(yt_id)
                if not data:
                    continue
                conn.execute(
                    """UPDATE short_stats
                       SET estimated_minutes_watched = ?,
                           average_view_duration = ?
                       WHERE id = (
                           SELECT MAX(id) FROM short_stats
                           WHERE short_id = ? AND yt_video_id = ?
                       )""",
                    (
                        float(data.get("estimatedMinutesWatched", 0)),
                        float(data.get("averageViewDuration", 0)),
                        sid,
                        yt_id,
                    ),
                )
                count += conn.total_changes
            conn.commit()
        return count

    # ── Daily Watchtime (YPP tracking) ─────────────────────────

    def upsert_daily_watchtime(self, channel_id: int, daily_data: list[dict]) -> int:
        """Insert or update daily watch time records for a channel.

        Args:
            channel_id: Channel ID.
            daily_data: List of {date, estimatedMinutesWatched, subscribersGained}.

        Returns:
            Number of rows inserted/updated.
        """
        if not daily_data:
            return 0
        with self._connect() as conn:
            count = 0
            for row in daily_data:
                conn.execute(
                    """INSERT INTO channel_daily_watchtime
                       (channel_id, date, estimated_minutes_watched, subscribers_gained)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(channel_id, date) DO UPDATE SET
                           estimated_minutes_watched = excluded.estimated_minutes_watched,
                           subscribers_gained = excluded.subscribers_gained""",
                    (
                        channel_id,
                        row["date"],
                        row["estimatedMinutesWatched"],
                        row.get("subscribersGained", 0),
                    ),
                )
                count += 1
            conn.commit()
        return count

    def get_channel_daily_watchtime(
        self, channel_id: int, days: int = 365
    ) -> list[dict]:
        """Get daily watch time data for a channel.

        Args:
            channel_id: Channel ID.
            days: Lookback window in days.

        Returns:
            List of {date, estimated_minutes_watched, subscribers_gained}.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT date, estimated_minutes_watched, subscribers_gained
                   FROM channel_daily_watchtime
                   WHERE channel_id = ?
                   ORDER BY date ASC""",
                (channel_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_channel_watch_time_summary(self, channel_id: int) -> dict:
        """Get watch time summary for YPP monetization tracking.

        Returns cumulative watch hours, daily average, and 365-day projection.
        """
        with self._connect() as conn:
            # Total watch time from latest channel stats
            latest = conn.execute(
                """SELECT estimated_minutes_watched, subscribers
                   FROM channel_stats_history
                   WHERE channel_id = ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (channel_id,),
            ).fetchone()

            # Daily breakdown from channel_daily_watchtime
            daily = conn.execute(
                """SELECT date, estimated_minutes_watched, subscribers_gained
                   FROM channel_daily_watchtime
                   WHERE channel_id = ?
                     AND date >= date('now', '-365 days')
                   ORDER BY date ASC""",
                (channel_id,),
            ).fetchall()

            # Compute per-video watch hours
            video_watch = conn.execute(
                """SELECT v.id, v.titulo_final, v.yt_video_id, v.yt_url,
                          vsh.estimated_minutes_watched, vsh.views, vsh.likes
                   FROM videos v
                   JOIN video_stats_history vsh ON vsh.id = (
                       SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                       WHERE vsh2.video_id = v.id
                   )
                   WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL
                     AND vsh.estimated_minutes_watched > 0
                   ORDER BY vsh.estimated_minutes_watched DESC""",
                (channel_id,),
            ).fetchall()

        total_minutes = latest["estimated_minutes_watched"] if latest else 0
        total_hours = round(total_minutes / 60.0, 1)

        daily_data = [dict(r) for r in daily]
        cumulative_hours = 0.0
        for d in daily_data:
            cumulative_hours += d["estimated_minutes_watched"] / 60.0
            d["watch_hours"] = round(d["estimated_minutes_watched"] / 60.0, 2)
            d["cumulative_hours"] = round(cumulative_hours, 1)

        # Daily average from the last 30 days of daily data
        daily_avg_hours = 0.0
        recent_daily = [d for d in daily_data if d["estimated_minutes_watched"] > 0]
        if recent_daily:
            daily_avg_hours = round(
                sum(d["estimated_minutes_watched"] for d in recent_daily)
                / len(recent_daily)
                / 60.0,
                2,
            )

        # Projection to 4000h
        ypp_target = 4000
        remaining_hours = max(0, ypp_target - total_hours)
        estimated_days = None
        if daily_avg_hours > 0 and remaining_hours > 0:
            estimated_days = round(remaining_hours / daily_avg_hours)

        video_data = []
        for v in video_watch:
            vd = dict(v)
            vd["watch_hours"] = round((vd["estimated_minutes_watched"] or 0) / 60.0, 1)
            video_data.append(vd)

        return {
            "channel_id": channel_id,
            "total_watch_hours": total_hours,
            "ypp_target_hours": ypp_target,
            "ypp_progress_pct": round(min(100, total_hours / ypp_target * 100), 1),
            "remaining_hours": remaining_hours,
            "daily_avg_hours": daily_avg_hours,
            "estimated_days_to_4000h": estimated_days,
            "daily_breakdown": daily_data,
            "top_videos_by_watchtime": video_data[:10],
        }

    # ── Channel Stats History ──────────────────────────────────

    def insert_channel_stats(self, channel_id: int, stats: dict) -> int | None:
        """Insert a snapshot of YouTube channel statistics."""
        emw = stats.get("estimatedMinutesWatched")
        emw_val = float(emw) if emw is not None and emw not in (0, "0") else 0.0
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO channel_stats_history
                   (channel_id, subscribers, total_views, video_count, estimated_minutes_watched,
                    estimated_revenue_min, estimated_revenue_max)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    channel_id,
                    int(stats.get("subscriberCount", 0)),
                    int(stats.get("viewCount", 0)),
                    int(stats.get("videoCount", 0)),
                    emw_val,
                    float(stats.get("estimated_revenue_min", 0)),
                    float(stats.get("estimated_revenue_max", 0)),
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

    # ── Optimal Publish Slots (v10) ────────────────────────────

    def upsert_optimal_slot(self, channel_id: int, content_type: str, slot_rank: int,
                             target_hour: int, timezone: str, score: float = 0.0,
                             confidence: float = 0.0, target_minute: int = 0,
                             audience_focus: str = 'blend', metrics_snapshot: str = '{}',
                             data_sources: str = '{}', audience_split: str = '{}') -> int | None:
        """Insert or update a single optimal publish slot."""
        # Clamp slot_rank to valid range [1, 5] — calculator may produce
        # out-of-range values during migrations or edge cases
        slot_rank = max(1, min(5, int(slot_rank)))
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO optimal_publish_slots
                   (channel_id, content_type, slot_rank, target_hour, target_minute, timezone,
                    score, confidence, audience_focus, metrics_snapshot, data_sources,
                    audience_split, calculated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(channel_id, content_type, slot_rank) DO UPDATE SET
                    target_hour=excluded.target_hour,
                    target_minute=excluded.target_minute,
                    timezone=excluded.timezone,
                    score=excluded.score,
                    confidence=excluded.confidence,
                    audience_focus=excluded.audience_focus,
                    metrics_snapshot=excluded.metrics_snapshot,
                    data_sources=excluded.data_sources,
                    audience_split=excluded.audience_split,
                    calculated_at=datetime('now')""",
                (channel_id, content_type, slot_rank, target_hour, target_minute, timezone,
                 score, confidence, audience_focus, metrics_snapshot, data_sources,
                 audience_split),
            )
            conn.commit()
            return cursor.lastrowid

    def get_optimal_slots(self, channel_id: int, content_type: str = None) -> list[dict]:
        """Get all optimal publish slots for a channel, optionally filtered by content_type.
        
        Returns slots ordered by slot_rank (1=best, 2, 3).
        Only returns slots calculated within the last 48 hours.
        """
        with self._connect() as conn:
            if content_type:
                rows = conn.execute(
                    """SELECT * FROM optimal_publish_slots
                       WHERE channel_id = ? AND content_type = ?
                         AND calculated_at >= datetime('now', '-48 hours')
                       ORDER BY slot_rank ASC""",
                    (channel_id, content_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM optimal_publish_slots
                       WHERE channel_id = ?
                         AND calculated_at >= datetime('now', '-48 hours')
                       ORDER BY content_type, slot_rank ASC""",
                    (channel_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_optimal_slot_assignment(self, channel_id: int, content_type: str) -> dict | None:
        """Get the next optimal slot to use using epsilon-greedy strategy.
        
        70% of the time: round-robin (pick next least-used slot)
        30% of the time: exploitation (pick slot with best avg_views_result)
        Falls back to slot_rank=1 if no data.
        
        Returns the selected slot row or None.
        """
        import random
        slots = self.get_optimal_slots(channel_id, content_type)
        if not slots:
            return None
        
        # Exploitation 30%: pick slot with best real performance
        if random.random() < 0.3:
            best = max(slots, key=lambda s: s.get("avg_views_result", 0) or 0)
            if best.get("used_count", 0) > 0:
                return best
        
        # Exploration 70%: round-robin (least-used among the 3)
        return min(slots, key=lambda s: s.get("used_count", 0))

    def record_slot_usage(self, channel_id: int, content_type: str, slot_rank: int) -> bool:
        """Increment used_count for a slot. Called when a video/short is assigned to this slot."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE optimal_publish_slots
                   SET used_count = used_count + 1
                   WHERE channel_id = ? AND content_type = ? AND slot_rank = ?""",
                (channel_id, content_type, slot_rank),
            )
            conn.commit()
            return True

    def record_slot_result(self, channel_id: int, content_type: str, slot_rank: int,
                            video_views: int) -> bool:
        """Record actual view results for a slot assignment.
        
        Updates total_views_result and recalculates moving average.
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT used_count, total_views_result FROM optimal_publish_slots
                   WHERE channel_id = ? AND content_type = ? AND slot_rank = ?""",
                (channel_id, content_type, slot_rank),
            ).fetchone()
            if not row:
                return False
            used = (row["used_count"] or 0)
            total = (row["total_views_result"] or 0) + video_views
            avg = total / max(used, 1)
            conn.execute(
                """UPDATE optimal_publish_slots
                   SET total_views_result = ?, avg_views_result = ?
                   WHERE channel_id = ? AND content_type = ? AND slot_rank = ?""",
                (total, avg, channel_id, content_type, slot_rank),
            )
            conn.commit()
            return True

    def clear_stale_optimal_slots(self, channel_id: int = None) -> int:
        """Delete optimal slots older than 7 days. Returns count deleted."""
        with self._connect() as conn:
            if channel_id:
                cursor = conn.execute(
                    """DELETE FROM optimal_publish_slots
                       WHERE channel_id = ?
                         AND calculated_at < datetime('now', '-7 days')""",
                    (channel_id,),
                )
            else:
                cursor = conn.execute(
                    """DELETE FROM optimal_publish_slots
                       WHERE calculated_at < datetime('now', '-7 days')"""
                )
            conn.commit()
            return cursor.rowcount

    def get_channel_latest_stats(self, channel_id: int) -> dict | None:
        """Get the most recent stats snapshot for a single channel."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM channel_stats_history
                   WHERE channel_id = ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_video_latest_stats(self, video_id: int) -> dict | None:
        """Get the most recent stats snapshot for a single video."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM video_stats_history
                   WHERE video_id = ?
                   ORDER BY fetched_at DESC LIMIT 1""",
                (video_id,),
            ).fetchone()
        return dict(row) if row else None
    
    def get_all_channels_latest_stats(self) -> list[dict]:
        """Get the most recent stats snapshot for every channel (one row per channel)."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT csh.*, ch.name as channel_name, ch.slug as channel_slug,
                          (SELECT COUNT(*) FROM shorts s WHERE s.channel_id = ch.id AND s.status = 'published') as shorts_published,
                          (SELECT COUNT(*) FROM shorts s WHERE s.channel_id = ch.id) as shorts_total,
                          (SELECT COALESCE(SUM(vsh.likes), 0) FROM videos v
                             JOIN video_stats_history vsh ON vsh.id = (
                               SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                               WHERE vsh2.video_id = v.id AND vsh2.likes > 0
                             )
                             WHERE v.channel_id = ch.id) as total_likes,
                          (SELECT COALESCE(SUM(vsh.views), 0) FROM videos v
                             JOIN video_stats_history vsh ON vsh.id = (
                               SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                               WHERE vsh2.video_id = v.id AND vsh2.views > 0
                             )
                              WHERE v.channel_id = ch.id AND v.yt_video_id IS NOT NULL) as longform_views,
                           (SELECT COALESCE(SUM(ss.views), 0) FROM shorts s
                              JOIN short_stats ss ON ss.id = (
                                SELECT MAX(ss2.id) FROM short_stats ss2
                                WHERE ss2.short_id = s.id AND ss2.views > 0
                              )
                              WHERE s.channel_id = ch.id AND s.status = 'published' AND s.youtube_id IS NOT NULL) as shorts_views,
                           (SELECT COALESCE(SUM(ss.likes), 0) FROM shorts s
                              JOIN short_stats ss ON ss.id = (
                                SELECT MAX(ss2.id) FROM short_stats ss2
                                WHERE ss2.short_id = s.id AND ss2.likes > 0
                              )
                              WHERE s.channel_id = ch.id AND s.status = 'published' AND s.youtube_id IS NOT NULL) as shorts_likes
                    FROM channel_stats_history csh
                    JOIN channels ch ON ch.id = csh.channel_id
                    WHERE csh.id IN (
                       SELECT MAX(csh2.id) FROM channel_stats_history csh2
                       GROUP BY csh2.channel_id
                   )
                   ORDER BY ch.name"""
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Monetization ──────────────────────────────────────────

    def get_channel_monetization(self, channel_id: int) -> dict | None:
        """Get monetization config (cpm_min, cpm_max, vertical) for a channel."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, slug, cpm_min, cpm_max, monetization_vertical, ypp_status FROM channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_channel_monetization(self, channel_id: int, cpm_min: float = None,
                                     cpm_max: float = None, vertical: str = None) -> bool:
        """Update CPM config for a channel."""
        fields, values = [], []
        if cpm_min is not None:
            fields.append("cpm_min = ?"); values.append(cpm_min)
        if cpm_max is not None:
            fields.append("cpm_max = ?"); values.append(cpm_max)
        if vertical is not None:
            fields.append("monetization_vertical = ?"); values.append(vertical)
        if not fields:
            return False
        values.append(channel_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE channels SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
        return True

    def get_channel_revenue_total(self, channel_id: int) -> dict:
        """Sum estimated revenue from latest video stats for a channel."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(vsh.estimated_revenue_min), 0) as total_min,
                          COALESCE(SUM(vsh.estimated_revenue_max), 0) as total_max,
                          COALESCE(SUM(vsh.views), 0) as total_longform_views
                   FROM videos v
                   JOIN video_stats_history vsh ON vsh.id = (
                       SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                       WHERE vsh2.video_id = v.id AND vsh2.views > 0
                   )
                   WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL""",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else {"total_min": 0, "total_max": 0, "total_longform_views": 0}

    # ── Milestones ───────────────────────────────────────────

    def upsert_channel_milestone(self, channel_id: int, metric_type: str,
                                  target_value: float, label: str, tier: str = "standard",
                                  sort_order: int = 0, status: str = "in_progress",
                                  achieved_at: str = None) -> int:
        """Insert or update a channel milestone row."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO channel_milestones
                   (channel_id, metric_type, target_value, label, tier, sort_order, status, achieved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(channel_id, metric_type, target_value) DO UPDATE SET
                       status = excluded.status,
                       achieved_at = COALESCE(excluded.achieved_at, channel_milestones.achieved_at)""",
                (channel_id, metric_type, target_value, label, tier, sort_order, status, achieved_at),
            )
            conn.commit()
            return cursor.lastrowid

    def get_channel_milestones(self, channel_id: int) -> list[dict]:
        """Get all milestones for a channel, ordered by sort_order."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM channel_milestones
                   WHERE channel_id = ? ORDER BY sort_order""",
                (channel_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_upcoming_milestones(self, limit: int = 8) -> list[dict]:
        """Get milestones not yet achieved across all active channels."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT cm.*, ch.name as channel_name, ch.slug as channel_slug
                   FROM channel_milestones cm
                   JOIN channels ch ON cm.channel_id = ch.id
                   WHERE cm.status = 'in_progress' AND ch.active = 1
                   ORDER BY cm.sort_order LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Video Analytics Detailed ─────────────────────────────

    def insert_video_analytics_batch(self, video_id: int, yt_video_id: str,
                                      report_type: str, rows_data: list[dict]) -> int:
        """Insert analytics rows (traffic sources, demographics, etc.) for a video."""
        count = 0
        with self._connect() as conn:
            # Remove old data for this video+report_type before inserting fresh
            conn.execute(
                "DELETE FROM video_analytics_detailed WHERE video_id = ? AND report_type = ?",
                (video_id, report_type),
            )
            for item in rows_data:
                conn.execute(
                    """INSERT INTO video_analytics_detailed
                       (video_id, yt_video_id, report_type, dimension, metric_value)
                       VALUES (?, ?, ?, ?, ?)""",
                    (video_id, yt_video_id, report_type,
                     item.get("dimension"), item.get("metric_value", 0)),
                )
                count += 1
            conn.commit()
        return count

    def get_video_analytics(self, video_id: int) -> list[dict]:
        """Get all analytics data for a video."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM video_analytics_detailed
                   WHERE video_id = ? ORDER BY report_type, metric_value DESC""",
                (video_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Growth Data for Charts ───────────────────────────────

    def get_channel_growth_data(self, channel_id: int, days: int = 30) -> list[dict]:
        """Get daily stats snapshots for a channel's growth chart."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DATE(fetched_at) as date_key,
                          MAX(subscribers) as subscribers,
                          MAX(total_views) as total_views,
                          MAX(estimated_minutes_watched) as watch_minutes,
                          MAX(estimated_revenue_min) as revenue_min,
                          MAX(estimated_revenue_max) as revenue_max
                   FROM channel_stats_history
                   WHERE channel_id = ?
                     AND fetched_at >= datetime('now', ?)
                   GROUP BY DATE(fetched_at)
                   ORDER BY date_key ASC""",
                (channel_id, f"-{days} days"),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_channel_content_ranking(self, channel_id: int, limit: int = 20) -> list[dict]:
        """Rank videos by views with revenue and retention data."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT v.id, v.titulo_final, v.yt_video_id, v.yt_url, v.duracion_seg,
                          v.created_at,
                          vsh.views, vsh.likes, vsh.comments,
                          vsh.estimated_minutes_watched, vsh.average_view_duration,
                          vsh.subscribers_gained, vsh.estimated_revenue_min,
                          vsh.estimated_revenue_max, vsh.fetched_at as stats_updated
                   FROM videos v
                   JOIN video_stats_history vsh ON vsh.id = (
                       SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                       WHERE vsh2.video_id = v.id AND vsh2.views > 0
                   )
                   WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL
                   ORDER BY vsh.views DESC LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_dashboard_data(self, channel_id: int = None) -> dict:
        """Unified dashboard data: KPIs, channels, pipeline, upcoming, top videos.
        If channel_id is provided, filters all data to that channel."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row

            ch_filter = "AND v.channel_id = ?" if channel_id else ""
            ch_params = (channel_id,) if channel_id else ()

            # ── Engagement per channel (likes + comments from latest video stats, real Data API v3 data) ──
            engagement_sql = f"""SELECT v.channel_id,
                          COALESCE(SUM(vsh.likes + vsh.comments), 0) as engagement
                   FROM videos v
                   JOIN video_stats_history vsh ON vsh.id = (
                       SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                       WHERE vsh2.video_id = v.id AND (vsh2.likes > 0 OR vsh2.comments > 0)
                   )
                   WHERE v.yt_video_id IS NOT NULL {ch_filter.replace('v.channel_id', 'v.channel_id')}
                   GROUP BY v.channel_id"""
            engagement_by_channel = conn.execute(engagement_sql, ch_params).fetchall()
            engagement_map = {r["channel_id"]: r["engagement"] for r in engagement_by_channel}

            # ── Engagement from shorts (likes + comments from latest short_stats) ──
            shorts_eng_sql = f"""SELECT s.channel_id,
                          COALESCE(SUM(ss.likes + ss.comments), 0) as engagement
                   FROM shorts s
                   JOIN short_stats ss ON ss.id = (
                       SELECT MAX(ss2.id) FROM short_stats ss2
                       WHERE ss2.short_id = s.id AND (ss2.likes > 0 OR ss2.comments > 0)
                   )
                   WHERE s.status = 'published' AND s.youtube_id IS NOT NULL
                     {ch_filter.replace('v.channel_id', 's.channel_id')}
                   GROUP BY s.channel_id"""
            shorts_engagement_by_channel = conn.execute(shorts_eng_sql, ch_params).fetchall()
            shorts_engagement_map = {r["channel_id"]: r["engagement"] for r in shorts_engagement_by_channel}

            # ── Channel comparison with latest stats ──
            ch_where = "AND ch.id = ?" if channel_id else ""
            channels_sql = f"""SELECT ch.id, ch.name, ch.slug, ch.active,
                          ch.cpm_min, ch.cpm_max, ch.monetization_vertical, ch.ypp_status,
                          csh.subscribers, csh.total_views, csh.video_count,
                          csh.estimated_minutes_watched, csh.fetched_at as stats_updated,
                          csh.estimated_revenue_min, csh.estimated_revenue_max,
                          (SELECT COUNT(*) FROM videos v WHERE v.channel_id = ch.id AND v.yt_video_id IS NOT NULL) as uploaded_videos,
                          (SELECT COUNT(*) FROM shorts s WHERE s.channel_id = ch.id AND s.status = 'published') as shorts_published,
                          (SELECT COUNT(*) FROM shorts s WHERE s.channel_id = ch.id) as shorts_total,
                          (SELECT COALESCE(SUM(vsh.likes), 0) FROM videos v
                             JOIN video_stats_history vsh ON vsh.id = (
                               SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                               WHERE vsh2.video_id = v.id AND vsh2.likes > 0
                             )
                             WHERE v.channel_id = ch.id AND v.yt_video_id IS NOT NULL) as total_likes,
                          (SELECT COALESCE(SUM(vsh.views), 0) FROM videos v
                             JOIN video_stats_history vsh ON vsh.id = (
                               SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                               WHERE vsh2.video_id = v.id AND vsh2.views > 0
                             )
                              WHERE v.channel_id = ch.id AND v.yt_video_id IS NOT NULL) as longform_views,
                           (SELECT COALESCE(SUM(ss.views), 0) FROM shorts s
                              JOIN short_stats ss ON ss.id = (
                                SELECT MAX(ss2.id) FROM short_stats ss2
                                WHERE ss2.short_id = s.id AND ss2.views > 0
                              )
                              WHERE s.channel_id = ch.id AND s.status = 'published' AND s.youtube_id IS NOT NULL) as shorts_views,
                           (SELECT COALESCE(SUM(ss.likes), 0) FROM shorts s
                              JOIN short_stats ss ON ss.id = (
                                SELECT MAX(ss2.id) FROM short_stats ss2
                                WHERE ss2.short_id = s.id AND ss2.likes > 0
                              )
                              WHERE s.channel_id = ch.id AND s.status = 'published' AND s.youtube_id IS NOT NULL) as shorts_likes
                    FROM channels ch
                   LEFT JOIN channel_stats_history csh ON csh.id = (
                       SELECT MAX(csh2.id) FROM channel_stats_history csh2 WHERE csh2.channel_id = ch.id
                   )
                   WHERE ch.active = 1 {ch_where}
                   ORDER BY ch.name"""
            channels = conn.execute(channels_sql, ch_params).fetchall()

            channels_data = []
            total_subscribers = 0
            total_views = 0
            total_engagement_longform = 0
            total_engagement_shorts = 0
            total_shorts_published = 0
            total_likes = 0
            total_longform_views = 0
            total_shorts_views = 0
            total_revenue_min = 0
            total_revenue_max = 0
            total_watch_hours = 0.0
            total_watch_hours_prev = 0.0
            subscribers_prev = 0
            views_prev = 0
            engagement_prev = 0

            for ch in channels:
                ch_dict = dict(ch)
                ch_dict["engagement"] = engagement_map.get(ch_dict["id"], 0)
                ch_dict["shorts_engagement"] = shorts_engagement_map.get(ch_dict["id"], 0)
                channels_data.append(ch_dict)
                total_subscribers += (ch_dict["subscribers"] or 0)
                total_views += (ch_dict["total_views"] or 0)
                total_engagement_longform += ch_dict["engagement"]
                total_engagement_shorts += ch_dict["shorts_engagement"]
                total_shorts_published += (ch_dict["shorts_published"] or 0)
                total_likes += (ch_dict["total_likes"] or 0)
                total_longform_views += (ch_dict["longform_views"] or 0)
                total_shorts_views += (ch_dict["shorts_views"] or 0)
                total_revenue_min += (ch_dict.get("estimated_revenue_min") or 0)
                total_revenue_max += (ch_dict.get("estimated_revenue_max") or 0)

                # Compute YPP progress per channel
                subs = ch_dict.get("subscribers") or 0
                watch_hours = round((ch_dict.get("estimated_minutes_watched") or 0) / 60.0, 1)
                ch_dict["ypp_subs_pct"] = min(100, round(subs / 10, 1))  # target 1000
                ch_dict["ypp_hours_pct"] = min(100, round(watch_hours / 40, 1))  # target 4000
                ch_dict["watch_hours"] = watch_hours
                total_watch_hours += watch_hours

                # Get previous snapshot (~7 days ago) for delta
                prev = conn.execute(
                    """SELECT subscribers, total_views, estimated_minutes_watched
                       FROM channel_stats_history
                       WHERE channel_id = ? AND fetched_at <= datetime('now', '-7 days')
                       ORDER BY fetched_at DESC LIMIT 1""",
                    (ch_dict["id"],),
                ).fetchone()
                if prev:
                    subscribers_prev += (prev["subscribers"] or 0)
                    views_prev += (prev["total_views"] or 0)
                    total_watch_hours_prev += round((prev["estimated_minutes_watched"] or 0) / 60.0, 1)

                # Engagement at ~7 days ago
                eng_prev = conn.execute(
                    """SELECT COALESCE(SUM(vsh.likes + vsh.comments), 0) as engagement
                       FROM videos v
                       JOIN video_stats_history vsh ON vsh.id = (
                           SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                           WHERE vsh2.video_id = v.id
                             AND (vsh2.likes > 0 OR vsh2.comments > 0)
                             AND vsh2.fetched_at <= datetime('now', '-7 days')
                       )
                       WHERE v.yt_video_id IS NOT NULL AND v.channel_id = ?""",
                    (ch_dict["id"],),
                ).fetchone()
                if eng_prev:
                    engagement_prev += (eng_prev["engagement"] or 0)

                # Shorts engagement at ~7 days ago
                shorts_eng_prev = conn.execute(
                    """SELECT COALESCE(SUM(ss.likes + ss.comments), 0) as engagement
                       FROM shorts s
                       JOIN short_stats ss ON ss.id = (
                           SELECT MAX(ss2.id) FROM short_stats ss2
                           WHERE ss2.short_id = s.id
                             AND (ss2.likes > 0 OR ss2.comments > 0)
                             AND ss2.fetched_at <= datetime('now', '-7 days')
                       )
                       WHERE s.status = 'published' AND s.youtube_id IS NOT NULL
                         AND s.channel_id = ?""",
                    (ch_dict["id"],),
                ).fetchone()
                if shorts_eng_prev:
                    engagement_prev += (shorts_eng_prev["engagement"] or 0)

            def _delta_pct(current, previous):
                if previous and previous > 0 and current is not None and current > 0:
                    return round((current - previous) / previous * 100, 1)
                return None

            # ── Pipeline: videos generating or ready ──
            pipe_where = "AND v.channel_id = ?" if channel_id else ""
            pipeline = conn.execute(
                f"""SELECT v.id, v.titulo_final, v.status, v.progress, v.progress_phase,
                          v.created_at, c.name as channel_name, c.slug as channel_slug
                   FROM videos v
                   JOIN channels c ON v.channel_id = c.id
                   WHERE v.status IN ('generating', 'ready', 'reassembling') {pipe_where.replace('v.channel_id', 'v.channel_id')}
                   ORDER BY v.created_at DESC LIMIT 10""",
                ch_params,
            ).fetchall()

            in_prod_where = "AND channel_id = ?" if channel_id else ""
            in_production = conn.execute(
                f"SELECT COUNT(*) as c FROM videos WHERE status = 'generating' {in_prod_where}",
                ch_params,
            ).fetchone()["c"]
            ready_count = conn.execute(
                f"SELECT COUNT(*) as c FROM videos WHERE status = 'ready' {in_prod_where}",
                ch_params,
            ).fetchone()["c"]

            # ── Upcoming schedules ──
            up_where = "AND cs.channel_id = ?" if channel_id else ""
            upcoming = conn.execute(
                f"""SELECT cs.*, c.name as channel_name, c.slug as channel_slug,
                           v.titulo_final as video_title
                    FROM content_schedules cs
                    JOIN channels c ON cs.channel_id = c.id
                    LEFT JOIN videos v ON cs.video_id = v.id
                    WHERE cs.active = 1 AND cs.next_run_at > datetime('now','localtime') {up_where}
                    ORDER BY cs.next_run_at ASC LIMIT 8""",
                ch_params,
            ).fetchall()

            # ── Top 5 videos by views ──
            top_where = "AND v.channel_id = ?" if channel_id else ""
            top_videos = conn.execute(
                f"""SELECT v.id, v.titulo_final, v.yt_video_id, v.yt_url, v.duracion_seg,
                           v.video_path, v.created_at, c.name as channel_name, c.slug as channel_slug,
                           vsh.views, vsh.likes, vsh.comments, vsh.estimated_minutes_watched,
                           vsh.average_view_duration, vsh.estimated_revenue_min,
                           vsh.estimated_revenue_max, vsh.fetched_at as stats_updated
                    FROM videos v
                    JOIN channels c ON v.channel_id = c.id
                    JOIN video_stats_history vsh ON vsh.id = (
                        SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                        WHERE vsh2.video_id = v.id AND vsh2.views > 0
                    )
                    WHERE 1=1 {top_where}
                    ORDER BY vsh.views DESC LIMIT 5""",
                ch_params,
            ).fetchall()

            # ── Sparkline data for KPI cards (last 8 days) ──
            sparkline_subscribers = []
            sparkline_views = []
            sparkline_engagement = []
            sparkline_watch_hours = []
            spark_ch_filter = "AND csh.channel_id = ?" if channel_id else ""
            for days_ago in range(7, -1, -1):
                date_point = f"datetime('now', '-{days_ago} days')"
                aggr = conn.execute(
                    f"""SELECT SUM(csh.subscribers) as subs, SUM(csh.total_views) as views,
                               SUM(csh.estimated_minutes_watched) as watch_minutes
                        FROM channel_stats_history csh
                        INNER JOIN (
                            SELECT channel_id, MAX(id) as max_id
                            FROM channel_stats_history
                            WHERE fetched_at <= {date_point}
                            GROUP BY channel_id
                        ) latest ON csh.id = latest.max_id
                        WHERE 1=1 {spark_ch_filter}""",
                    ch_params,
                ).fetchone()
                eng_where = "AND v.channel_id = ?" if channel_id else ""
                eng_aggr = conn.execute(
                    f"""SELECT COALESCE(SUM(vsh.likes + vsh.comments), 0) as engagement
                        FROM videos v
                        JOIN video_stats_history vsh ON vsh.id = (
                            SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                            WHERE vsh2.video_id = v.id
                              AND (vsh2.likes > 0 OR vsh2.comments > 0)
                              AND vsh2.fetched_at <= {date_point}
                        )
                        WHERE v.yt_video_id IS NOT NULL {eng_where}""",
                    ch_params,
                ).fetchone()
                shorts_eng_where = "AND s.channel_id = ?" if channel_id else ""
                shorts_eng_aggr = conn.execute(
                    f"""SELECT COALESCE(SUM(ss.likes + ss.comments), 0) as engagement
                        FROM shorts s
                        JOIN short_stats ss ON ss.id = (
                            SELECT MAX(ss2.id) FROM short_stats ss2
                            WHERE ss2.short_id = s.id
                              AND (ss2.likes > 0 OR ss2.comments > 0)
                              AND ss2.fetched_at <= {date_point}
                        )
                        WHERE s.status = 'published' AND s.youtube_id IS NOT NULL {shorts_eng_where}""",
                    ch_params,
                ).fetchone()
                if aggr:
                    sparkline_subscribers.append(aggr["subs"] or 0)
                    sparkline_views.append(aggr["views"] or 0)
                    sparkline_engagement.append(
                        (eng_aggr["engagement"] or 0) + (shorts_eng_aggr["engagement"] or 0)
                    )
                    sparkline_watch_hours.append(
                        round((aggr["watch_minutes"] or 0) / 60.0, 1)
                    )

            # ── Recent published videos (last 10, all statuses) ──
            rec_where = "AND v.channel_id = ?" if channel_id else ""
            recent_videos = conn.execute(
                f"""SELECT v.id, v.titulo_final, v.yt_video_id, v.yt_url, v.duracion_seg, v.uploaded_at,
                           v.status, c.name as channel_name, c.slug as channel_slug
                    FROM videos v
                    JOIN channels c ON v.channel_id = c.id
                    WHERE 1=1 {rec_where}
                    ORDER BY COALESCE(v.uploaded_at, v.created_at) DESC
                    LIMIT 10""",
                ch_params,
            ).fetchall()

            # ── Videos published today (last 10, all statuses) ──
            today_videos = conn.execute(
                f"""SELECT v.id, v.titulo_final, v.yt_video_id, v.yt_url, v.duracion_seg, v.uploaded_at,
                           v.status, c.name as channel_name, c.slug as channel_slug
                    FROM videos v
                    JOIN channels c ON v.channel_id = c.id
                    WHERE COALESCE(v.uploaded_at, v.created_at) >= datetime('now', 'localtime', '-1 day')
                      {rec_where}
                    ORDER BY COALESCE(v.uploaded_at, v.created_at) DESC
                    LIMIT 10""",
                ch_params,
            ).fetchall()

            # ── Recent shorts (last 10, all statuses) ──
            recs_where = "AND s.channel_id = ?" if channel_id else ""
            recent_shorts = conn.execute(
                f"""SELECT s.id, s.title, s.youtube_id, s.youtube_url, s.duration, s.published_at,
                           s.status, c.name as channel_name, c.slug as channel_slug
                    FROM shorts s
                    JOIN channels c ON s.channel_id = c.id
                    WHERE 1=1 {recs_where}
                    ORDER BY COALESCE(s.published_at, s.created_at) DESC
                    LIMIT 10""",
                ch_params,
            ).fetchall()

            # ── Heatmap data: daily views last 30 days per channel ──
            heatmap_data = []
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            for days_back in range(29, -1, -1):
                target = today - timedelta(days=days_back)
                day_start = target.strftime("%Y-%m-%d")
                day_end   = (target + timedelta(days=1)).strftime("%Y-%m-%d")
                day_date  = target.strftime("%Y-%m-%d")
                per_channel = {}
                hd_channels = conn.execute("SELECT id FROM channels WHERE active = 1").fetchall()
                for hch in hd_channels:
                    total_day = conn.execute(
                        """SELECT COALESCE(SUM(vsh.views), 0) as v
                           FROM videos v
                           JOIN video_stats_history vsh ON vsh.id = (
                               SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                               WHERE vsh2.video_id = v.id
                                 AND vsh2.fetched_at >= ? AND vsh2.fetched_at < ?
                           )
                           WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL""",
                        (day_start, day_end, hch["id"]),
                    ).fetchone()
                    per_channel[str(hch["id"])] = total_day["v"] or 0
                heatmap_data.append({
                    "date": day_date,
                    "total_views": sum(per_channel.values()),
                    "channels": per_channel,
                })

            # ── Streaks: rachas activas ──
            streaks_sql = "SELECT * FROM streaks"
            streaks_params = ()
            if channel_id:
                streaks_sql += " WHERE channel_id = ?"
                streaks_params = (channel_id,)
            streaks = [dict(r) for r in conn.execute(streaks_sql, streaks_params).fetchall()]

            # ── Badges: logros desbloqueados ──
            badges_sql = "SELECT * FROM badges"
            badges_params = ()
            if channel_id:
                badges_sql += " WHERE channel_id = ?"
                badges_params = (channel_id,)
            badges = [dict(r) for r in conn.execute(badges_sql, badges_params).fetchall()]

            # ── Shorts pipeline: today's status counts ──
            shorts_pipe_where = "AND sps.channel_id = ?" if channel_id else ""
            shorts_pipeline = conn.execute(
                f"""SELECT sps.status, COUNT(*) as count
                    FROM shorts_planned_slots sps
                    JOIN channels ch ON sps.channel_id = ch.id
                    WHERE sps.date_key = date('now', 'localtime')
                      AND ch.active = 1 {shorts_pipe_where}
                    GROUP BY sps.status""",
                ch_params,
            ).fetchall()
            shorts_pipeline_data = {}
            for r in shorts_pipeline:
                shorts_pipeline_data[r["status"]] = r["count"]

        return {
            "global_kpis": {
                "subscribers": {
                    "value": total_subscribers,
                    "delta": _delta_pct(total_subscribers, subscribers_prev),
                },
                "total_views": {
                    "value": total_views,
                    "delta": _delta_pct(total_views, views_prev),
                    "breakdown": {
                        "longform": total_longform_views,
                        "shorts": total_shorts_views,
                    },
                },
                "engagement": {
                    "value": total_engagement_longform + total_engagement_shorts,
                    "delta": _delta_pct(total_engagement_longform + total_engagement_shorts, engagement_prev),
                    "breakdown": {
                        "longform": total_engagement_longform,
                        "shorts": total_engagement_shorts,
                    },
                },
                "total_likes": {
                    "value": total_likes,
                    "delta": None,
                },
                "shorts": {
                    "value": total_shorts_published,
                    "delta": None,
                },
                "in_production": {
                    "value": in_production + ready_count,
                    "generating": in_production,
                    "ready": ready_count,
                },
                "watch_hours": {
                    "value": total_watch_hours,
                    "delta": _delta_pct(total_watch_hours, total_watch_hours_prev),
                },
                "sparkline_subscribers": sparkline_subscribers,
                "sparkline_views": sparkline_views,
                "sparkline_engagement": sparkline_engagement,
                "sparkline_watch_hours": sparkline_watch_hours,
            },
            "channels": channels_data,
            "pipeline": [dict(r) for r in pipeline],
            "shorts_pipeline": shorts_pipeline_data,
            "upcoming": [dict(r) for r in upcoming],
            "top_videos": [dict(r) for r in top_videos],
            "recent_videos": [dict(r) for r in recent_videos],
            "recent_shorts": [dict(r) for r in recent_shorts],
            "today_videos": [dict(r) for r in today_videos],
            "ypp_progress": channels_data,
            "revenue_overview": {
                "total_min": round(total_revenue_min, 2),
                "total_max": round(total_revenue_max, 2),
                "avg_cpm_min": round(total_revenue_min / max(total_longform_views, 1) * 1000, 2),
                "avg_cpm_max": round(total_revenue_max / max(total_longform_views, 1) * 1000, 2),
            },
            "heatmap_data": heatmap_data,
            "streaks": streaks,
            "badges": badges,
            "today_actions": self._get_today_actions(conn, channel_id, ch_params),
        }

    def _get_today_actions(self, conn, channel_id=None, ch_params=()):
        """Return a unified timeline of actions that occurred today.

        Each action represents a discrete event: video generated, video uploaded,
        video published, short generated, short published. Multiple actions can
        refer to the same entity (e.g. a video generated and uploaded the same day).

        Includes errored generations so operators can still see they were attempted.
        """
        v_where = "AND v.channel_id = ?" if channel_id else ""
        s_where = "AND s.channel_id = ?" if channel_id else ""

        sql = f"""
            -- Videos generated today (any status, including errors)
            SELECT v.id as entity_id, 'video' as entity_type,
                   'generated' as action,
                   v.created_at as action_at,
                   v.titulo_final as title, v.status,
                   c.name as channel_name, c.slug as channel_slug,
                   v.yt_video_id as yt_id
            FROM videos v
            JOIN channels c ON v.channel_id = c.id
            WHERE v.created_at >= datetime('now', 'localtime', '-1 day')
              {v_where.replace('v.channel_id', 'v.channel_id')}

            UNION ALL

            -- Videos uploaded today
            SELECT v.id, 'video',
                   'uploaded',
                   v.uploaded_at,
                   v.titulo_final, v.status,
                   c.name, c.slug,
                   v.yt_video_id
            FROM videos v
            JOIN channels c ON v.channel_id = c.id
            WHERE v.uploaded_at IS NOT NULL
              AND v.uploaded_at >= datetime('now', 'localtime', '-1 day')
              {v_where.replace('v.channel_id', 'v.channel_id')}

            UNION ALL

            -- Videos made public today
            SELECT v.id, 'video',
                   'published',
                   v.published_at,
                   v.titulo_final, v.status,
                   c.name, c.slug,
                   v.yt_video_id
            FROM videos v
            JOIN channels c ON v.channel_id = c.id
            WHERE v.published_at IS NOT NULL
              AND v.published_at >= datetime('now', 'localtime', '-1 day')
              {v_where.replace('v.channel_id', 'v.channel_id')}

            UNION ALL

            -- Shorts generated today
            SELECT s.id, 'short',
                   'generated',
                   s.created_at,
                   s.title, s.status,
                   c.name, c.slug,
                   s.youtube_id
            FROM shorts s
            JOIN channels c ON s.channel_id = c.id
            WHERE s.created_at >= datetime('now', 'localtime', '-1 day')
              {s_where.replace('s.channel_id', 's.channel_id')}

            UNION ALL

            -- Shorts published today (always public)
            SELECT s.id, 'short',
                   'published',
                   s.published_at,
                   s.title, s.status,
                   c.name, c.slug,
                   s.youtube_id
            FROM shorts s
            JOIN channels c ON s.channel_id = c.id
            WHERE s.published_at IS NOT NULL
              AND s.published_at >= datetime('now', 'localtime', '-1 day')
              {s_where.replace('s.channel_id', 's.channel_id')}

            ORDER BY action_at DESC
        """
        rows = conn.execute(sql, ch_params * 5).fetchall()
        return [dict(r) for r in rows]

    # ── Channel Templates ─────────────────────────────────────

    def get_channel_templates(self, channel_id: int) -> list[dict]:
        """Get all templates for a channel."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM channel_templates WHERE channel_id = ?", (channel_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_channel_template(self, channel_id: int, segment_type: str) -> Optional[dict]:
        """Get a specific template for a channel."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_templates WHERE channel_id = ? AND segment_type = ?",
                (channel_id, segment_type)
            ).fetchone()
            return dict(row) if row else None

    # ── Planning Slots ──────────────────────────────────────────
    
    def create_planned_slot(self, channel_id: int, date_key: str, scheduled_at: str,
                            target_upload_at: str = None, slot_position: int = 0,
                            source_mode: str = "original",
                            target_public_at: str = None,
                            upload_window_start: int = 9,
                            upload_window_end: int = 11) -> int:
        """Create a planned slot. Returns slot id."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO planned_slots (channel_id, date_key, scheduled_at,
                   target_upload_at, target_public_at, upload_window_start, upload_window_end,
                   slot_position, source_mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel_id, date_key, scheduled_at, target_upload_at,
                 target_public_at, upload_window_start, upload_window_end,
                 slot_position, source_mode),
            )
            conn.commit()
            return cursor.lastrowid
    
    def create_planned_slots_batch(self, slots: list[dict]) -> int:
        """Insert multiple slots atomically. Returns count inserted."""
        count = 0
        with self._connect() as conn:
            for s in slots:
                conn.execute(
                    """INSERT INTO planned_slots (channel_id, date_key, scheduled_at,
                       target_upload_at, target_public_at, upload_window_start, upload_window_end,
                       slot_position, source_mode)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (s["channel_id"], s["date_key"], s["scheduled_at"],
                     s.get("target_upload_at"), s.get("target_public_at"),
                     s.get("upload_window_start", 9), s.get("upload_window_end", 11),
                     s.get("slot_position", 0),
                     s.get("source_mode", "original")),
                )
                count += 1
            conn.commit()
        return count
    
    def get_planned_slots(self, date_key: str = None, channel_id: int = None,
                          status: str = None) -> list[dict]:
        """Get planned slots with optional filters."""
        q = """SELECT ps.*, c.name as channel_name, c.slug as channel_slug
               FROM planned_slots ps
               JOIN channels c ON ps.channel_id = c.id
               WHERE 1=1"""
        params = []
        if date_key:
            q += " AND ps.date_key = ?"; params.append(date_key)
        if channel_id:
            q += " AND ps.channel_id = ?"; params.append(channel_id)
        if status:
            q += " AND ps.status = ?"; params.append(status)
        q += " ORDER BY ps.scheduled_at ASC"
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    
    def get_planned_slot_for_video(self, video_id: int) -> dict | None:
        """Get the planned_slot linked to a video via generation_jobs."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT ps.* FROM planned_slots ps
                   JOIN generation_jobs gj ON ps.job_id = gj.id
                   WHERE gj.video_id = ?
                   ORDER BY ps.id DESC LIMIT 1""",
                (video_id,),
            ).fetchone()
        return dict(row) if row else None
    
    def get_planned_slots_week(self, start_date: str, end_date: str,
                                channel_id: int = None) -> list[dict]:
        """Get planned slots for a date range."""
        q = """SELECT ps.*, c.name as channel_name, c.slug as channel_slug
               FROM planned_slots ps
               JOIN channels c ON ps.channel_id = c.id
               WHERE ps.date_key >= ? AND ps.date_key <= ?"""
        params = [start_date, end_date]
        if channel_id:
            q += " AND ps.channel_id = ?"; params.append(channel_id)
        q += " ORDER BY ps.date_key, ps.scheduled_at ASC"
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    
    # ── Dispatch backoff helpers (v12) ────────────────────────────────────
    
    @staticmethod
    def _is_slot_in_cooldown(slot: dict) -> bool:
        """Check if a pending slot is in cooldown after a failed dispatch attempt.
        
        Returns True if the slot should be skipped (too recent failure).
        Exponential backoff: attempt 1 → 5min, 2 → 15min, 3+ → 60min.
        Slots with 3+ failed_attempts and no successful retry are considered
        poisoned and will be cancelled by _cancel_stale_slots().
        """
        failed = slot.get("failed_attempts", 0) or 0
        if failed <= 0:
            return False
        last = slot.get("last_failed_at")
        if not last:
            return False
        try:
            from datetime import datetime, timedelta
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            # Exponential backoff: attempt 1 → 5min, 2 → 15min, 3+ → 60min
            _COOLDOWNS = [5, 15, 60]
            idx = min(failed - 1, len(_COOLDOWNS) - 1)
            cooldown_min = _COOLDOWNS[max(0, idx)]
            if datetime.now(tz=last_dt.tzinfo) - last_dt < timedelta(minutes=cooldown_min):
                return True
        except (ValueError, TypeError):
            pass
        return False
    
    # Adjust import to make helper usable as module-level, but it's a staticmethod
    # so it also works via ExtendedDatabase._is_slot_in_cooldown().
    
    def get_next_pending_slot(self) -> dict | None:
        """Get the next pending slot that is due (scheduled_at <= now), 
        ordered by scheduled_at. Returns None if none.
        
        Skips slots in dispatch cooldown (recently failed with exponential backoff).
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT ps.*, c.name as channel_name, c.slug as channel_slug
                   FROM planned_slots ps
                   JOIN channels c ON ps.channel_id = c.id
                   WHERE ps.status = 'pending'
                     AND ps.scheduled_at <= datetime('now', 'localtime')
                   ORDER BY ps.scheduled_at ASC LIMIT 20"""
            ).fetchall()
        for row in rows:
            slot = dict(row)
            if not self._is_slot_in_cooldown(slot):
                return slot
        return None
    
    def get_next_available_slot(self, max_future_hours: int = 36) -> dict | None:
        """Get the first pending slot whose target_public_at is within
        max_future_hours from now. Supports pull-forward dispatch.
        
        Unlike get_next_pending_slot(), this does NOT require scheduled_at <= now,
        allowing generation to start early when the worker is idle.
        
        ORDERING: Past-due slots (scheduled_at <= now) come first, sorted by
        target_public_at ASC (earliest upload date = most urgent). Pull-forward
        slots (future scheduled_at within lead window) come after, also sorted
        by target_public_at ASC. This ensures the video that should have been
        published earliest gets generated first.
        
        IMPORTANT: excludes slots where date_key is in the past (yesterday or
        earlier). Past-date slots have missed their upload window entirely and
        should be cancelled, not dispatched.
        
        Skips slots in dispatch cooldown (v12 backoff).
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT ps.*, c.name as channel_name, c.slug as channel_slug
                   FROM planned_slots ps
                   JOIN channels c ON ps.channel_id = c.id
                   WHERE ps.status = 'pending'
                      AND ps.date_key >= date('now', 'localtime')
                      AND (
                          -- Due slot: scheduled_at has passed
                          ps.scheduled_at <= datetime('now', 'localtime')
                          OR
                          -- Pull-forward: target upload is within the lead window
                          (ps.target_public_at IS NOT NULL
                           AND ps.target_public_at <= datetime('now', 'localtime', ? || ' hours'))
                      )
                   ORDER BY
                      CASE WHEN ps.scheduled_at <= datetime('now', 'localtime') THEN 0 ELSE 1 END,
                      COALESCE(ps.target_public_at, ps.target_upload_at) ASC
                   LIMIT 20""",
                (f"+{max_future_hours}",),
            ).fetchall()
        for row in rows:
            slot = dict(row)
            if not self._is_slot_in_cooldown(slot):
                return slot
        return None
    
    def get_priority_slot_candidates(self, max_future_hours: int = 36,
                                     limit: int = 20) -> list[dict]:
        """Get pending slots due or within pull-forward window, for priority scoring.
        
        Returns ALL matching slots (not just one) so the dispatcher can score
        them and pick the best one. Includes channel slug and extra metadata.
        
        ORDERED by target_public_at ASC so the scorer can prioritize by date urgency.
        
        Skips slots in dispatch cooldown (v12 backoff).
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT ps.*, c.name as channel_name, c.slug as channel_slug,
                          c.config_json
                   FROM planned_slots ps
                   JOIN channels c ON ps.channel_id = c.id
                   WHERE ps.status = 'pending'
                     AND ps.date_key >= date('now', 'localtime')
                     AND (
                         ps.scheduled_at <= datetime('now', 'localtime')
                         OR
                         (ps.target_public_at IS NOT NULL
                          AND ps.target_public_at <= datetime('now', 'localtime', ? || ' hours'))
                     )
                   ORDER BY
                     CASE WHEN ps.scheduled_at <= datetime('now', 'localtime') THEN 0 ELSE 1 END,
                     COALESCE(ps.target_public_at, ps.target_upload_at) ASC
                   LIMIT ?""",
                (f"+{max_future_hours}", limit),
            ).fetchall()
        candidates = [dict(r) for r in rows]
        # Filter out slots in cooldown
        return [s for s in candidates if not self._is_slot_in_cooldown(s)]
    
    def count_videos_generated_today(self, channel_id: int) -> int:
        """Count videos generated, uploading, or uploaded today for a channel."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM videos
                   WHERE channel_id = ?
                     AND date(created_at) = date('now', 'localtime')
                     AND status IN ('generating', 'awaiting_upload', 'uploading',
                                    'uploaded', 'uploaded_private', 'published')""",
                (channel_id,),
            ).fetchone()
        return row["cnt"] if row else 0
    
    def count_completed_videos_for_date(self, channel_id: int, date_key: str) -> int:
        """Count long-form videos published/uploaded for a specific date_key.
        
        Used by shorts scheduler to determine how many clip slots to create
        (2 clips per long-form video published yesterday).
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM videos v
                   JOIN planned_slots p ON p.video_id = v.id
                   WHERE p.channel_id = ? AND p.date_key = ?
                   AND v.status IN ('uploaded', 'published', 'uploaded_private')""",
                (channel_id, date_key),
            ).fetchone()
        return row["cnt"] if row else 0
    
    def count_awaiting_upload(self, channel_id: int) -> int:
        """Count videos in awaiting_upload status for a channel."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM videos WHERE channel_id=? AND status='awaiting_upload'",
                (channel_id,),
            ).fetchone()
        return row["cnt"] if row else 0
    
    def count_slots_by_status(self, date_key: str, status: str) -> int:
        """Count slots by status for a specific date."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE date_key = ? AND status = ?",
                (date_key, status),
            ).fetchone()
        return row["cnt"] if row else 0
    
    def get_channel_slots_today(self, channel_id: int, date_key: str) -> list[dict]:
        """Get all slots (any status) for a channel on a specific date."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM planned_slots
                   WHERE channel_id = ? AND date_key = ?
                   ORDER BY scheduled_at ASC""",
                (channel_id, date_key),
            ).fetchall()
        return [dict(r) for r in rows]
    
    def update_slot_status(self, slot_id: int, status: str,
                           job_id: int = None, video_id: int = None) -> bool:
        """Update a planned slot's status and optionally link job/video."""
        fields = ["status = ?"]
        values = [status]
        if job_id is not None:
            fields.append("job_id = ?"); values.append(job_id)
        if video_id is not None:
            fields.append("video_id = ?"); values.append(video_id)
        values.append(slot_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE planned_slots SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
        return True
    
    def update_slot_source_mode(self, slot_id: int, source_mode: str) -> bool:
        """Update a planned slot's source_mode ('original' or 'viral')."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE planned_slots SET source_mode = ? WHERE id = ?",
                (source_mode, slot_id),
            )
            conn.commit()
        return True

    def increment_slot_failed_attempts(self, slot_id: int) -> int:
        """Record a failed dispatch attempt on a slot and return new attempt count.
        
        Increments failed_attempts, sets last_failed_at to now, and resets
        job_id to NULL (so the next dispatch creates a fresh job).
        
        Returns the new failed_attempts count.
        """
        with self._connect() as conn:
            conn.execute(
                """UPDATE planned_slots 
                   SET failed_attempts = COALESCE(failed_attempts, 0) + 1,
                       last_failed_at = datetime('now', 'localtime'),
                       job_id = NULL
                   WHERE id = ?""",
                (slot_id,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT failed_attempts FROM planned_slots WHERE id = ?", (slot_id,)
            ).fetchone()
            return (row["failed_attempts"] or 0) if row else 0

    def record_slot_dispatch_failure(self, job_id: int) -> str | None:
        """Record a failed dispatch on the slot linked to this job.
        
        Finds the slot by job_id, increments failed_attempts, sets last_failed_at.
        If failed_attempts >= 3, cancels the slot permanently.
        Otherwise resets to 'pending' so it can be retried after cooldown.
        
        Returns: 'cancelled' if slot was cancelled (permanent), 
                 'cooldown' if reset to pending with backoff,
                 None if no slot found for this job.
        """
        with self._connect() as conn:
            # Find the slot linked to this job
            slot_row = conn.execute(
                "SELECT id, failed_attempts FROM planned_slots WHERE job_id = ? AND status = 'running'",
                (job_id,),
            ).fetchone()
            if not slot_row:
                return None
            slot_id = slot_row["id"]
            prev_failed = slot_row["failed_attempts"] or 0
            new_failed = prev_failed + 1

            # Exponential backoff: attempt 1→5min, 2→15min, 3+→cancel
            if new_failed >= 4:  # 4th+ attempt = permanently cancel
                conn.execute(
                    "UPDATE planned_slots SET status = 'cancelled', job_id = NULL, "
                    "failed_attempts = ?, last_failed_at = datetime('now', 'localtime') "
                    "WHERE id = ?",
                    (new_failed, slot_id),
                )
                conn.commit()
                return 'cancelled'
            else:
                # Reset to pending with backoff info
                conn.execute(
                    "UPDATE planned_slots SET status = 'pending', job_id = NULL, "
                    "failed_attempts = ?, last_failed_at = datetime('now', 'localtime') "
                    "WHERE id = ?",
                    (new_failed, slot_id),
                )
                conn.commit()
                return 'cooldown'

    def cancel_slots(self, slot_ids: list[int]) -> int:
        """Cancel multiple planned slots. Returns count."""
        if not slot_ids:
            return 0
        placeholders = ",".join(["?" for _ in slot_ids])
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE planned_slots SET status = 'cancelled' WHERE id IN ({placeholders})",
                slot_ids,
            )
            conn.commit()
            return cursor.rowcount
    
    def prune_old_slots(self, before_date: str = None) -> dict:
        """Delete cancelled/skipped slots from before the given date (default: yesterday).
        
        Also prunes today's cancelled/skipped slots if the count exceeds the noise
        threshold (>50), which indicates replan churn rather than genuine cancellations.
        
        Returns a dict with counts of deleted rows from each table.
        Only prunes slots with status cancelled or skipped — never touches
        pending, running, or completed slots.
        """
        import logging
        from datetime import date as _dt, timedelta as _td
        logger = logging.getLogger("autotube.prune")
        
        if before_date is None:
            before_date = (_dt.today() - _td(days=1)).isoformat()
        
        result = {"planned_slots_deleted": 0, "shorts_planned_slots_deleted": 0,
                   "today_noise_cleared": 0, "today_shorts_noise_cleared": 0}
        
        MAX_TODAY_CANCELLED = 50  # Above this threshold, today's cancelled are replan noise
        
        with self._connect() as conn:
            # ── Prune planned_slots from old dates ──
            cursor = conn.execute(
                """DELETE FROM planned_slots
                    WHERE status IN ('cancelled', 'skipped')
                      AND date_key < ?""",
                (before_date,),
            )
            result["planned_slots_deleted"] = cursor.rowcount
            
            # ── Safety valve: prune today's excessive cancelled planned_slots ──
            today = _dt.today().isoformat()
            today_count = conn.execute(
                "SELECT COUNT(*) FROM planned_slots WHERE status IN ('cancelled','skipped') AND date_key = ?",
                (today,),
            ).fetchone()[0]
            
            if today_count > MAX_TODAY_CANCELLED:
                cursor_today = conn.execute(
                    """DELETE FROM planned_slots
                        WHERE status IN ('cancelled', 'skipped')
                          AND date_key = ?""",
                    (today,),
                )
                result["today_noise_cleared"] = cursor_today.rowcount
                logger.warning(
                    "Cleared %d excessive cancelled slots from today (threshold: %d)",
                    cursor_today.rowcount, MAX_TODAY_CANCELLED,
                )
            
            # ── Prune shorts_planned_slots ──
            try:
                cursor2 = conn.execute(
                    """DELETE FROM shorts_planned_slots
                        WHERE status IN ('cancelled', 'skipped', 'failed')
                          AND date_key < ?""",
                    (before_date,),
                )
                result["shorts_planned_slots_deleted"] = cursor2.rowcount
                
                # Safety valve for shorts too
                today_shorts = conn.execute(
                    "SELECT COUNT(*) FROM shorts_planned_slots WHERE status IN ('cancelled','skipped','failed') AND date_key = ?",
                    (today,),
                ).fetchone()[0]
                if today_shorts > MAX_TODAY_CANCELLED:
                    cursor_st = conn.execute(
                        """DELETE FROM shorts_planned_slots
                            WHERE status IN ('cancelled', 'skipped', 'failed')
                              AND date_key = ?""",
                        (today,),
                    )
                    result["today_shorts_noise_cleared"] = cursor_st.rowcount
            except Exception:
                pass  # Table may not exist
            
            conn.commit()
        
        total_deleted = result["planned_slots_deleted"] + result["today_noise_cleared"] + result["shorts_planned_slots_deleted"] + result["today_shorts_noise_cleared"]
        if total_deleted:
            logger.info(
                "Slot prune: %d planned-old + %d planned-today + %d shorts-old + %d shorts-today deleted",
                result["planned_slots_deleted"], result["today_noise_cleared"],
                result["shorts_planned_slots_deleted"], result["today_shorts_noise_cleared"],
            )
        
        return result
    
    def get_channel_planning_config(self, channel_id: int) -> dict:
        """Extract planning-related fields from channel config_json."""
        ch = self.get_channel(channel_id)
        if not ch:
            return {}
        try:
            config = json.loads(ch.get("config_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            config = {}
        return {
            "channel_id": channel_id,
            "channel_name": ch.get("name", ""),
            "slug": ch.get("slug", ""),
            "videos_per_day": config.get("videos_per_day", 1),
            "planning_enabled": config.get("planning_enabled", True),
            # ── Scheduled publishing config ──
            "publish_mode": config.get("PUBLISH_MODE", "immediate"),
            "publish_target_hour": config.get("PUBLISH_TARGET_HOUR"),
            "publish_jitter_min": config.get("PUBLISH_JITTER_MIN", 20),
            "publish_warmup_min": config.get("PUBLISH_WARMUP_MIN", 120),
            "publish_timezone": config.get("PUBLISH_TIMEZONE", "Europe/Madrid"),
            "seo_primary_keyword": config.get("SEO_PRIMARY_KEYWORD", ""),
            "seo_secondary_keywords": config.get("SEO_SECONDARY_KEYWORDS", []),
            # ── Alternate pattern (e.g. [2, 3] → alternates 2/3 daily) ──
            "alternate_pattern": config.get("alternate_pattern"),
            "alternate_offset": config.get("alternate_offset", 0),
            # ── Source mode distribution (videos_per_day = total, viral_per_day = how many viral) ──
            "viral_per_day": config.get("viral_per_day", 0),
            # ── Random daily boost weights (v9.1) ──
            "videos_day_boost_weight": config.get("videos_day_boost_weight", 0.7),
            "viral_day_boost_weight": config.get("viral_day_boost_weight", 0.2),
            # ── 3-phase pipeline config (v9) ──
            "upload_window_start": config.get("UPLOAD_WINDOW_START", 9),
            "upload_window_end": config.get("UPLOAD_WINDOW_END", 11),
            "generation_lead_hours": config.get("GENERATION_LEAD_HOURS", 36),
            # ── Multi-window upload (v11) ──
            "upload_windows": config.get("UPLOAD_WINDOWS", [
                {"start": config.get("UPLOAD_WINDOW_START", 9),
                 "end": config.get("UPLOAD_WINDOW_END", 11)}
            ]),
            "publish_window_spread_min": config.get("PUBLISH_WINDOW_SPREAD_MIN",
                                                     config.get("PUBLISH_JITTER_MIN", 20)),
        }
    
    _UNSET = object()  # sentinel to distinguish "not passed" from "explicitly clear"

    def update_channel_planning_config(self, channel_id: int,
                                        videos_per_day: int = None,
                                        planning_enabled: bool = None,
                                        alternate_pattern: list = _UNSET,
                                        alternate_offset: int = None,
                                        viral_per_day: int = None,
                                        upload_window_start: int = None,
                                        upload_window_end: int = None,
                                        generation_lead_hours: int = None,
                                        upload_windows: list = None,
                                        publish_window_spread_min: int = None,
                                        videos_day_boost_weight: float = None,
                                        viral_day_boost_weight: float = None) -> bool:
        """Update planning fields in channel config_json.

        Pass alternate_pattern=None (the Python value) to explicitly clear it.
        Omit the parameter (leaving it at sentinel _UNSET) to leave untouched.
        """
        ch = self.get_channel(channel_id)
        if not ch:
            return False
        try:
            config = json.loads(ch.get("config_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            config = {}
        if videos_per_day is not None:
            config["videos_per_day"] = max(0, min(10, videos_per_day))
        if planning_enabled is not None:
            config["planning_enabled"] = planning_enabled
        if alternate_pattern is not self._UNSET:
            config["alternate_pattern"] = alternate_pattern  # None → cleared
        if alternate_offset is not None:
            config["alternate_offset"] = alternate_offset
        if viral_per_day is not None:
            # Clamp: 0 <= viral_per_day <= videos_per_day (current total)
            total = config.get("videos_per_day", 1)
            config["viral_per_day"] = max(0, min(total, viral_per_day))
        if upload_window_start is not None:
            config["UPLOAD_WINDOW_START"] = max(0, min(23, upload_window_start))
        if upload_window_end is not None:
            config["UPLOAD_WINDOW_END"] = max(0, min(23, upload_window_end))
        if generation_lead_hours is not None:
            config["GENERATION_LEAD_HOURS"] = max(1, min(72, generation_lead_hours))
        if upload_windows is not None:
            # Validate structure
            valid = []
            for w in upload_windows:
                if isinstance(w, dict) and "start" in w and "end" in w:
                    valid.append({"start": max(0, min(23, int(w["start"]))),
                                  "end": max(0, min(23, int(w["end"])))})
            config["UPLOAD_WINDOWS"] = valid if valid else [{"start": 9, "end": 11}]
        if publish_window_spread_min is not None:
            config["PUBLISH_WINDOW_SPREAD_MIN"] = max(10, min(180, publish_window_spread_min))
        if videos_day_boost_weight is not None:
            config["videos_day_boost_weight"] = round(max(0.0, min(1.0, videos_day_boost_weight)), 2)
        if viral_day_boost_weight is not None:
            config["viral_day_boost_weight"] = round(max(0.0, min(1.0, viral_day_boost_weight)), 2)
        return self.update_channel(channel_id, config=config)
    
    def get_active_job(self) -> dict | None:
        """Check if any generation job is currently running or queued. Returns it or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE status IN ('running','queued') LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    
    def get_active_shorts_job(self) -> dict | None:
        """Check if any short generation job is running/queued. Returns it or None.
        
        Distinct from get_active_job() which blocks on ANY job. Shorts can run
        concurrently with long-form videos (1 video + 1 short max at a time).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE status IN ('running','queued') "
                "AND action IN ('generate_native_short', 'generate_clip_short') LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
    
    def get_active_job_for_channel(self, channel_id: int) -> dict | None:
        """Check if a generation job is active for a specific channel. Returns it or None.
        
        This replaces the global get_active_job() guard with a per-channel check,
        allowing parallel generation across different channels while preventing
        concurrent Kokoro TTS for the same channel.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE channel_id = ? AND status IN ('running','queued') LIMIT 1",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else None
    
    def count_active_jobs(self) -> int:
        """Count ALL generation jobs currently running or queued.
        
        Strictly sequential — one job at a time system-wide for ANY type
        (long-form, shorts, clips, uploads). Prevents ffmpeg resource
        contention, race conditions in preflight cleanup, and black-screen
        renders caused by concurrent job interference.
        
        Counts both 'running' AND 'queued' to close the TOCTOU race window:
        a job created by process_planned_slots() may briefly be 'queued'
        before the async task sets it to 'running'. If another dispatcher
        checks during that window, it must see the pending job.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM generation_jobs "
                "WHERE status IN ('running', 'queued')"
            ).fetchone()
        return row["cnt"] if row else 0
    
    def count_render_phase_jobs(self) -> int:
        """Count long-form generation jobs currently in the render phase.
        
        The render phase (pipeline_phase='render') is the most RAM-intensive
        part of generation. Phase pipelining allows ONE job in render while
        another job runs prep phases (scrape→script→TTS→media).
        
        Returns count of running long-form jobs with pipeline_phase='render'.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM generation_jobs "
                "WHERE status IN ('running', 'queued') "
                "AND pipeline_phase = 'render' "
                "AND action NOT IN ('generate_native_short', 'generate_clip_short', 'upload_only')"
            ).fetchone()
        return row["cnt"] if row else 0
    
    def count_active_upload_jobs(self) -> int:
        """Count upload_only jobs currently running or queued."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM generation_jobs "
                "WHERE status IN ('running', 'queued') "
                "AND action = 'upload_only'"
            ).fetchone()
        return row["cnt"] if row else 0
    
    def count_past_due_slots(self) -> int:
        """Count pending planned_slots whose scheduled_at is in the past.
        
        Used by the scheduler loop to determine catch-up mode and adaptive
        sleep interval.  More past-due slots = faster tick = faster recovery.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots "
                "WHERE status = 'pending' "
                "AND scheduled_at <= datetime('now', 'localtime')"
            ).fetchone()
        return row["cnt"] if row else 0
    
    def get_active_upload_job_for_channel(self, channel_id: int) -> dict | None:
        """Check if a channel already has an active upload_only job."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs "
                "WHERE status IN ('running', 'queued') "
                "AND action = 'upload_only' "
                "AND channel_id = ? LIMIT 1",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else None
    
    # ── Cross-process TTS lock for Kokoro ────────────────────────
    
    def acquire_tts_lock(self, channel_id: int, job_id: int) -> bool:
        """Acquire the TTS lock for a channel. Returns True if acquired, False if already locked.
        
        This prevents two subprocess workers from running Kokoro TTS simultaneously
        for the same channel, which would cause RTF degradation and timeouts.
        Uses a DB table with UNIQUE constraint as a cross-process mutex.

        Before acquiring, cleans up any stale locks (>30 min old) to prevent
        permanent lock leaks after process crashes.
        """
        try:
            with self._connect() as conn:
                # Clean up stale locks (>30 minutes old) before attempting acquire
                conn.execute(
                    "DELETE FROM channel_tts_lock "
                    "WHERE locked_at < datetime('now', '-30 minutes')"
                )
                conn.commit()
                conn.execute(
                    "INSERT INTO channel_tts_lock (channel_id, job_id, locked_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (channel_id, job_id),
                )
                conn.commit()
            return True
        except Exception:
            return False
    
    def release_tts_lock(self, channel_id: int, job_id: int) -> None:
        """Release the TTS lock for a channel."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM channel_tts_lock WHERE channel_id = ? AND job_id = ?",
                    (channel_id, job_id),
                )
                conn.commit()
        except Exception:
            pass
    
    def is_tts_locked(self, channel_id: int) -> bool:
        """Check if the TTS lock is held for a channel."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM channel_tts_lock WHERE channel_id = ? LIMIT 1",
                (channel_id,),
            ).fetchone()
        return row is not None

    # ── Media file locks (v11: cross-job file deletion prevention) ──

    def lock_media_files(self, job_id: int, file_paths: list) -> int:
        """Register locks on media files for a job. Idempotent via UNIQUE constraint.
        Returns the number of files locked."""
        if not file_paths:
            return 0
        locked = 0
        try:
            with self._connect() as conn:
                for fp in file_paths:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO media_file_locks (job_id, file_path) VALUES (?, ?)",
                            (job_id, str(fp)),
                        )
                        locked += 1
                    except Exception:
                        pass
                conn.commit()
        except Exception:
            pass
        return locked

    def unlock_media_files(self, job_id: int) -> int:
        """Release all media file locks for a job. Returns count of released locks."""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM media_file_locks WHERE job_id = ?",
                    (job_id,),
                )
                conn.commit()
                return cur.rowcount
        except Exception:
            return 0

    def get_locked_file_paths(self) -> set:
        """Get all file paths locked by currently active jobs (running/queued)."""
        try:
            with self._connect() as conn:
                rows = conn.execute("""
                    SELECT DISTINCT ml.file_path
                    FROM media_file_locks ml
                    JOIN generation_jobs gj ON ml.job_id = gj.id
                    WHERE gj.status IN ('running', 'queued')
                """).fetchall()
                return {str(row[0]) for row in rows} if rows else set()
        except Exception:
            return set()

    def cleanup_stale_locks(self) -> int:
        """Delete locks for jobs that have finished (completed/failed/cancelled).
        Returns number of locks cleaned."""
        try:
            with self._connect() as conn:
                cur = conn.execute("""
                    DELETE FROM media_file_locks
                    WHERE job_id NOT IN (
                        SELECT id FROM generation_jobs WHERE status IN ('running', 'queued')
                    )
                """)
                conn.commit()
                return cur.rowcount
        except Exception:
            return 0

    def upsert_channel_template(self, channel_id: int, segment_type: str,
                                 video_path: str, image_path: str = None,
                                 config_json: str = None) -> int:
        """Insert or update a channel template. Returns template id."""
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM channel_templates WHERE channel_id = ? AND segment_type = ?",
                (channel_id, segment_type)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE channel_templates SET video_path=?, image_path=?, config_json=?, generated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (video_path, image_path, config_json, existing["id"])
                )
                return existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO channel_templates (channel_id, segment_type, video_path, image_path, config_json) VALUES (?,?,?,?,?)",
                    (channel_id, segment_type, video_path, image_path, config_json)
                )
                return cursor.lastrowid
    
    # ── Shorts ────────────────────────────────────────────────

    def get_shorts(
        self, channel_id: int = None, status: str = None,
        type_filter: str = None, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List shorts with optional filters."""
        with self._connect() as conn:
            q = """SELECT s.*, c.slug as channel_slug, c.name as channel_name,
                   v.titulo_final as source_title
                   FROM shorts s
                   JOIN channels c ON s.channel_id = c.id
                   LEFT JOIN videos v ON s.source_video_id = v.id
                   WHERE 1=1"""
            params = []
            if channel_id is not None:
                q += " AND s.channel_id = ?"; params.append(channel_id)
            if status:
                q += " AND s.status = ?"; params.append(status)
            if type_filter:
                q += " AND s.type = ?"; params.append(type_filter)
            q += " ORDER BY s.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_short(self, short_id: int) -> Optional[dict]:
        """Get a single short by ID."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT s.*, c.slug as channel_slug, c.name as channel_name,
                   v.titulo_final as source_title
                   FROM shorts s
                   JOIN channels c ON s.channel_id = c.id
                   LEFT JOIN videos v ON s.source_video_id = v.id
                   WHERE s.id = ?""",
                (short_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_clips_for_video(self, video_id: int) -> list[dict]:
        """Get all clip shorts for a source video."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM shorts WHERE source_video_id = ? AND type = 'clip' ORDER BY ranking",
                (video_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_today_pending_shorts(self, channel_id: int = None) -> list[dict]:
        """Get all shorts scheduled for publication today."""
        from datetime import date
        today = date.today().isoformat()
        with self._connect() as conn:
            q = """SELECT s.*, c.slug as channel_slug, c.name as channel_name
                   FROM shorts s
                   JOIN channels c ON s.channel_id = c.id
                   WHERE s.scheduled_date = ? AND s.status = 'pending'"""
            params = [today]
            if channel_id is not None:
                q += " AND s.channel_id = ?"; params.append(channel_id)
            q += " ORDER BY s.ranking ASC"
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_recent_short_topics(self, channel_id: int, limit: int = 15) -> list[str]:
        """Get recently published native short topics for a channel.

        Returns a de-duplicated list of topic strings ordered most-recent-first.
        Used to avoid repeating themes across successive native shorts.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT topic FROM shorts
                   WHERE channel_id = ? AND type = 'native' AND topic IS NOT NULL AND topic != ''
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
        return [r["topic"] for r in rows]

    def get_shorts_stats(self) -> dict:
        """Get aggregate shorts statistics including YouTube metrics."""
        with self._connect() as conn:
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
            by_type = {}
            for row in conn.execute(
                "SELECT type, COUNT(*) as cnt FROM shorts GROUP BY type"
            ).fetchall():
                by_type[row["type"]] = row["cnt"]

            # YouTube metrics for published shorts
            yt_stats = conn.execute(
                """SELECT COALESCE(SUM(ss.views), 0) as total_views,
                          COALESCE(SUM(ss.likes), 0) as total_likes,
                          COALESCE(SUM(ss.comments), 0) as total_comments
                   FROM shorts s
                   JOIN short_stats ss ON ss.id = (SELECT MAX(ss2.id) FROM short_stats ss2
                                                    WHERE ss2.short_id = s.id)
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
                     SELECT s2.id, ss.views, ss.likes
                     FROM shorts s2
                     JOIN short_stats ss ON ss.id = (SELECT MAX(ss2.id) FROM short_stats ss2
                                                      WHERE ss2.short_id = s2.id)
                     WHERE s2.status = 'published' AND s2.youtube_id IS NOT NULL
                   ) yt ON yt.id = s.id
                   GROUP BY s.channel_id
                   ORDER BY c.name"""
            ).fetchall():
                per_channel.append(dict(row))
        return {
            "total": total,
            "published": published,
            "pending": pending,
            "ready": ready,
            "by_type": by_type,
            "total_views": yt_stats["total_views"] if yt_stats else 0,
            "total_likes": yt_stats["total_likes"] if yt_stats else 0,
            "total_comments": yt_stats["total_comments"] if yt_stats else 0,
            "per_channel": per_channel,
        }

    def get_channel_shorts_stats(self, channel_id: int) -> dict:
        """Get shorts aggregate stats for a specific channel including YouTube metrics."""
        with self._connect() as conn:
            counts = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM shorts WHERE channel_id = ? GROUP BY status",
                (channel_id,)
            ).fetchall():
                counts[row["status"]] = row["cnt"]

            yt_stats = conn.execute(
                """SELECT COALESCE(SUM(ss.views), 0) as total_views,
                          COALESCE(SUM(ss.likes), 0) as total_likes,
                          COALESCE(SUM(ss.comments), 0) as total_comments
                   FROM shorts s
                   JOIN short_stats ss ON ss.id = (SELECT MAX(ss2.id) FROM short_stats ss2
                                                    WHERE ss2.short_id = s.id)
                   WHERE s.channel_id = ? AND s.status = 'published' AND s.youtube_id IS NOT NULL""",
                (channel_id,)
            ).fetchone()

        return {
            "total": sum(counts.values()),
            "published": counts.get("published", 0),
            "pending": counts.get("pending", 0),
            "ready": counts.get("ready", 0),
            "rendering": counts.get("rendering", 0),
            "failed": counts.get("failed", 0),
            "total_views": yt_stats["total_views"] if yt_stats else 0,
            "total_likes": yt_stats["total_likes"] if yt_stats else 0,
            "total_comments": yt_stats["total_comments"] if yt_stats else 0,
        }
    
    def get_channel_videos_aggregate(self, channel_id: int) -> dict:
        """Aggregate stats from long-form videos for a specific channel.

        Returns sum of views/likes/comments from latest video_stats_history
        snapshot for each uploaded video, plus video count.
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(vsh.views), 0) as total_views,
                          COALESCE(SUM(vsh.likes), 0) as total_likes,
                          COALESCE(SUM(vsh.comments), 0) as total_comments,
                          COUNT(DISTINCT v.id) as video_count
                   FROM videos v
                   JOIN video_stats_history vsh ON vsh.id = (
                       SELECT MAX(vsh2.id) FROM video_stats_history vsh2
                       WHERE vsh2.video_id = v.id AND vsh2.views > 0
                   )
                   WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL""",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else {"total_views": 0, "total_likes": 0, "total_comments": 0, "video_count": 0}

    # ═══════════════════════════════════════════════════════════════
    # v5 — Promotion / Lifecycle
    # ═══════════════════════════════════════════════════════════════

    # ── YouTube Playlists ──────────────────────────────────────────

    def upsert_youtube_playlist(self, channel_id: int, slug: str,
                                 yt_playlist_id: str, name: str = None,
                                 playlist_type: str = 'thematic') -> int:
        """Insert or update a cached YouTube playlist ID. Returns row id."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO youtube_playlists (channel_id, slug, yt_playlist_id, name, playlist_type)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(channel_id, slug) DO UPDATE SET
                     yt_playlist_id = excluded.yt_playlist_id,
                     name = excluded.name,
                     playlist_type = excluded.playlist_type""",
                (channel_id, slug, yt_playlist_id, name, playlist_type),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM youtube_playlists WHERE channel_id = ? AND slug = ?",
                (channel_id, slug),
            ).fetchone()
            return row["id"] if row else 0

    def get_channel_youtube_playlists(self, channel_id: int) -> list[dict]:
        """Get all cached playlists for a channel."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM youtube_playlists WHERE channel_id = ? ORDER BY playlist_type, name",
                (channel_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_playlist_by_slug(self, channel_id: int, slug: str) -> Optional[dict]:
        """Get a cached playlist by channel + slug."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM youtube_playlists WHERE channel_id = ? AND slug = ?",
                (channel_id, slug),
            ).fetchone()
            return dict(row) if row else None

    def add_video_to_playlist_db(self, video_id: int, playlist_db_id: int,
                                  yt_playlist_item_id: str = None) -> int:
        """Record that a video was added to a playlist. Idempotent."""
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO video_playlists (video_id, playlist_id, yt_playlist_item_id)
                       VALUES (?, ?, ?)""",
                    (video_id, playlist_db_id, yt_playlist_item_id),
                )
                conn.commit()
            except Exception:
                pass
            row = conn.execute(
                "SELECT id FROM video_playlists WHERE video_id = ? AND playlist_id = ?",
                (video_id, playlist_db_id),
            ).fetchone()
            return row["id"] if row else 0

    def get_video_playlists_db(self, video_id: int) -> list[dict]:
        """Get all playlist assignments for a video (with playlist details)."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT vp.*, yp.slug as playlist_slug, yp.name as playlist_name,
                          yp.playlist_type, yp.yt_playlist_id
                   FROM video_playlists vp
                   JOIN youtube_playlists yp ON vp.playlist_id = yp.id
                   WHERE vp.video_id = ?
                   ORDER BY vp.added_at""",
                (video_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_video_playlists(self, video_id: int) -> int:
        """Remove all playlist assignments for a video. Returns deleted count."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM video_playlists WHERE video_id = ?", (video_id,)
            )
            conn.commit()
            return cursor.rowcount

    # ── Lifecycle Actions ──────────────────────────────────────────

    def create_lifecycle_action(self, video_id: int, action_type: str,
                                 channel_id: int, yt_video_id: str = None,
                                 scheduled_for: str = None,
                                 config_json: str = None) -> int:
        """Create a pending lifecycle action. Returns the new row id."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO video_lifecycle_actions
                   (video_id, action_type, channel_id, yt_video_id, scheduled_for, config_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (video_id, action_type, channel_id, yt_video_id, scheduled_for, config_json),
            )
            conn.commit()
            return cursor.lastrowid

    def get_due_lifecycle_actions(self) -> list[dict]:
        """Get all pending lifecycle actions whose scheduled_for has passed."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT vla.*, c.slug as channel_slug
                   FROM video_lifecycle_actions vla
                   JOIN channels c ON vla.channel_id = c.id
                   WHERE vla.status = 'pending'
                     AND datetime(vla.scheduled_for) <= datetime('now','localtime')
                   ORDER BY vla.scheduled_for ASC
                   LIMIT 50""",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_video_lifecycle_actions(self, video_id: int,
                                     status: str = None) -> list[dict]:
        """Get all lifecycle actions for a video, optionally filtered by status."""
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    """SELECT * FROM video_lifecycle_actions
                       WHERE video_id = ? AND status = ?
                       ORDER BY scheduled_for ASC""",
                    (video_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM video_lifecycle_actions
                       WHERE video_id = ?
                       ORDER BY scheduled_for ASC""",
                    (video_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def update_lifecycle_action_status(self, action_id: int, status: str,
                                        executed_at: str = None,
                                        result_json: str = None,
                                        error_message: str = None,
                                        retry_count: int = None,
                                        scheduled_for: str = None) -> bool:
        """Update a lifecycle action's status and results."""
        with self._connect() as conn:
            parts = ["status = ?"]
            params: list = [status]

            if executed_at is not None:
                parts.append("executed_at = ?")
                params.append(executed_at)
            if result_json is not None:
                parts.append("result_json = ?")
                params.append(result_json)
            if error_message is not None:
                parts.append("error_message = ?")
                params.append(error_message)
            if retry_count is not None:
                parts.append("retry_count = ?")
                params.append(retry_count)
            if scheduled_for is not None:
                parts.append("scheduled_for = ?")
                params.append(scheduled_for)

            params.append(action_id)
            conn.execute(
                f"UPDATE video_lifecycle_actions SET {', '.join(parts)} WHERE id = ?",
                params,
            )
            conn.commit()
            return True

    def cancel_pending_lifecycle_actions(self, video_id: int) -> int:
        """Cancel all pending actions for a video. Returns cancelled count."""
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE video_lifecycle_actions SET status = 'cancelled'
                   WHERE video_id = ? AND status = 'pending'""",
                (video_id,),
            )
            conn.commit()
            return cursor.rowcount

    def get_channel_latest_lifecycle(self, channel_id: int,
                                      limit: int = 30) -> list[dict]:
        """Get recent lifecycle actions for a channel's videos."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT vla.*, v.titulo_final as video_title
                   FROM video_lifecycle_actions vla
                   LEFT JOIN videos v ON vla.video_id = v.id
                   WHERE vla.channel_id = ?
                   ORDER BY vla.created_at DESC
                   LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Comment Log ────────────────────────────────────────────────

    def log_comment(self, video_id: int, yt_video_id: str,
                     yt_comment_id: str, comment_type: str = 'first',
                     parent_comment_id: str = None,
                     comment_text: str = None) -> int:
        """Record a posted comment. Returns new row id."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO comment_log (video_id, yt_video_id, yt_comment_id,
                   parent_comment_id, comment_type, comment_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (video_id, yt_video_id, yt_comment_id,
                 parent_comment_id, comment_type, comment_text),
            )
            conn.commit()
            return cursor.lastrowid

    def get_video_comments_log(self, video_id: int) -> list[dict]:
        """Get all comments posted for a video via the promotion system."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM comment_log
                   WHERE video_id = ?
                   ORDER BY posted_at DESC""",
                (video_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def has_first_comment(self, video_id: int) -> bool:
        """Check if a first comment has already been posted for this video."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM comment_log WHERE video_id = ? AND comment_type = 'first'",
                (video_id,),
            ).fetchone()
            return row["cnt"] > 0 if row else False

    # ── Shorts Planning Config ────────────────────────────────────

    def get_shorts_planning_config(self, channel_id: int = None) -> list[dict]:
        """Get shorts planning config for active channels, optionally filtered."""
        with self._connect() as conn:
            if channel_id:
                rows = conn.execute(
                    """SELECT spc.*, c.slug, c.name
                       FROM shorts_planning_config spc
                       JOIN channels c ON spc.channel_id = c.id
                       WHERE spc.channel_id = ?""",
                    (channel_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT spc.*, c.slug, c.name
                       FROM shorts_planning_config spc
                       JOIN channels c ON spc.channel_id = c.id
                       WHERE c.active = 1
                       ORDER BY c.name"""
                ).fetchall()
        configs = []
        for row in rows:
            r = dict(row)
            configs.append({
                "channel_id": r["channel_id"],
                "name": r["name"],
                "slug": r["slug"],
                "shorts_enabled": bool(r.get("shorts_enabled", 1)),
                "shorts_native_per_day": r.get("shorts_native_per_day", 3),
                "shorts_clip_per_day": r.get("shorts_clip_per_day", 2),
                "shorts_clips_per_long": r.get("shorts_clips_per_long", 3),
            })
        return configs

    def update_shorts_planning_config(self, channel_id: int, data: dict) -> bool:
        """Update shorts planning config for one channel.
        
        Accepted keys: shorts_enabled, shorts_native_per_day, shorts_clip_per_day, shorts_clips_per_long.
        """
        with self._connect() as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(shorts_planning_config)").fetchall()}
            fields = []
            values = []

            for k, v in data.items():
                col = None
                if k == "shorts_enabled":
                    col = "shorts_enabled"
                    v = 1 if v else 0
                elif k == "shorts_native_per_day":
                    col = "shorts_native_per_day"
                elif k == "shorts_clip_per_day":
                    col = "shorts_clip_per_day"
                elif k == "shorts_clips_per_long":
                    col = "shorts_clips_per_long"
                    v = max(0, min(5, v))
                if col and col in columns:
                    fields.append(f"{col} = ?")
                    values.append(v)

            if not fields:
                return False

            fields.append("updated_at = datetime('now','localtime')")
            values.append(channel_id)
            conn.execute(
                f"UPDATE shorts_planning_config SET {', '.join(fields)} WHERE channel_id = ?",
                values,
            )
            conn.commit()
            return True

    # ── Shorts Planned Slots ───────────────────────────────────────

    def create_shorts_slot(self, channel_id: int, date_key: str, scheduled_at: str,
                           target_upload_at: str = None, short_type: str = 'native',
                           long_slot_position: int = None, source_video_id: int = None,
                           slot_position: int = 0) -> int:
        """Create a single shorts planned slot. Returns slot id."""
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.execute(
                """INSERT INTO shorts_planned_slots
                   (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                    long_slot_position, source_video_id, slot_position)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                 long_slot_position, source_video_id, slot_position),
            )
            conn.commit()
            return cursor.lastrowid

    def create_shorts_planned_slots_batch(self, slots: list[dict]) -> int:
        """Insert multiple shorts slots atomically. Returns count inserted."""
        count = 0
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            for s in slots:
                conn.execute(
                    """INSERT INTO shorts_planned_slots
                       (channel_id, date_key, scheduled_at, target_upload_at,
                        short_type, slot_position, long_slot_position, source_video_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (s["channel_id"], s["date_key"], s["scheduled_at"],
                     s.get("target_upload_at"), s["short_type"],
                     s.get("slot_position", 0), s.get("long_slot_position"),
                     s.get("source_video_id")),
                )
                count += 1
            conn.commit()
        return count

    def get_shorts_planned_slots(self, date_key: str = None, channel_id: int = None,
                                  status: str = None) -> list[dict]:
        """Get shorts planned slots with optional filters."""
        q = """SELECT sps.*, c.name as channel_name, c.slug as channel_slug
               FROM shorts_planned_slots sps
               JOIN channels c ON sps.channel_id = c.id
               WHERE 1=1"""
        params = []
        if date_key:
            q += " AND sps.date_key = ?"; params.append(date_key)
        if channel_id:
            q += " AND sps.channel_id = ?"; params.append(channel_id)
        if status:
            q += " AND sps.status = ?"; params.append(status)
        q += " ORDER BY sps.scheduled_at ASC"
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_shorts_planned_slots_week(self, start_date: str, end_date: str,
                                       channel_id: int = None) -> list[dict]:
        """Get shorts planned slots for a date range."""
        q = """SELECT sps.*, c.name as channel_name, c.slug as channel_slug
               FROM shorts_planned_slots sps
               JOIN channels c ON sps.channel_id = c.id
               WHERE sps.date_key >= ? AND sps.date_key <= ?"""
        params = [start_date, end_date]
        if channel_id:
            q += " AND sps.channel_id = ?"; params.append(channel_id)
        q += " ORDER BY sps.date_key, sps.scheduled_at ASC"
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_next_pending_shorts_slot(self) -> dict | None:
        """Get the next pending short slot that is due (scheduled_at <= now),
        ordered by target_upload_at ASC (nearest upload date first). Returns
        None if none.
        
        Shorts with the closest target_upload_at are generated first, ensuring
        that the most time-sensitive shorts are interleaved between long-form
        videos without missing their publish window.
        
        Excludes past-date slots (date_key < today) to prevent obsolete
        slots from previous days from blocking the dispatch queue.
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT sps.*, c.name as channel_name, c.slug as channel_slug
                   FROM shorts_planned_slots sps
                   JOIN channels c ON sps.channel_id = c.id
                   WHERE sps.status = 'pending'
                      AND sps.date_key >= date('now', 'localtime')
                      AND sps.scheduled_at <= datetime('now', 'localtime')
                    ORDER BY COALESCE(sps.target_upload_at, sps.scheduled_at) ASC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def count_shorts_slots_by_status(self, date_key: str, status: str) -> int:
        """Count shorts slots by status for a specific date."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM shorts_planned_slots
                   WHERE date_key = ? AND status = ?""",
                (date_key, status),
            ).fetchone()
        return row["cnt"] if row else 0

    def get_shorts_published_today(self, channel_id: int) -> int:
        """Count shorts successfully published today for a channel.

        Used by the shorts recovery planner to determine how many of
        today's target shorts have already been published.

        Returns count of shorts where:
        - channel_id matches
        - youtube_id IS NOT NULL (successfully uploaded to YouTube)
        - status = 'published'
        - published_at is today (local time)
        """
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM shorts
                   WHERE channel_id = ?
                     AND youtube_id IS NOT NULL
                     AND status = 'published'
                     AND DATE(published_at) = DATE('now', 'localtime')""",
                (channel_id,),
            ).fetchone()
        return row["cnt"] if row else 0

    def get_channel_shorts_slots_today(self, channel_id: int, date_key: str) -> list[dict]:
        """Get all shorts slots for a channel on a specific date."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM shorts_planned_slots
                   WHERE channel_id = ? AND date_key = ?
                   ORDER BY scheduled_at ASC""",
                (channel_id, date_key),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_channel_last_short_completed_at(self, channel_id: int) -> str | None:
        """Get updated_at of the most recent completed short slot for a channel.

        Used by the cooldown guard to enforce minimum spacing between
        short generations per channel. Scans all dates, not just today.

        Returns ISO timestamp string or None if no completed shorts.
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT updated_at FROM shorts_planned_slots
                   WHERE channel_id = ? AND status = 'completed'
                   ORDER BY updated_at DESC LIMIT 1""",
                (channel_id,),
            ).fetchone()
        return row["updated_at"] if row and row["updated_at"] else None

    def update_shorts_slot_status(self, slot_id: int, status: str,
                                   source_video_id: int = None,
                                   short_id: int = None,
                                   job_id: int = None,
                                   error_message: str = None) -> bool:
        """Update a shorts planned slot's status and optionally link references."""
        fields = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        values = [status]
        if source_video_id is not None:
            fields.append("source_video_id = ?"); values.append(source_video_id)
        if short_id is not None:
            fields.append("short_id = ?"); values.append(short_id)
        if job_id is not None:
            fields.append("job_id = ?"); values.append(job_id)
        if error_message is not None:
            fields.append("error_message = ?"); values.append(error_message)
        values.append(slot_id)
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(
                f"UPDATE shorts_planned_slots SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
        return True

    def cancel_shorts_slots(self, slot_ids: list[int]) -> int:
        """Cancel multiple shorts slots. Returns count."""
        if not slot_ids:
            return 0
        placeholders = ",".join(["?" for _ in slot_ids])
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.execute(
                f"""UPDATE shorts_planned_slots
                   SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                   WHERE id IN ({placeholders})""",
                slot_ids,
            )
            conn.commit()
            return cursor.rowcount

    def get_pipeline_status(self) -> dict:
        """Return full pipeline status for the visual scheduling view.

        Returns 7 sections:
        - planned:        video slots pending today (not dispatched yet)
        - generating:     long-form videos currently being generated
        - awaiting_upload: videos generated locally, waiting for F2 upload window
        - warming:        videos uploaded as private waiting to go public
        - published_24h:  videos & shorts published (public) in the last 24 hours
        - shorts:         { pending, generating, completed }
        """
        result = {
            "planned": [], "generating": [], "awaiting_upload": [], "warming": [],
            "published_24h": [],
            "shorts": {"pending": [], "generating": [], "completed": []},
        }
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row

            # ── 1. Planned (pending slots for today, not dispatched yet) ──
            planned = conn.execute(
                """SELECT
                    ps.id as slot_id,
                    ps.channel_id,
                    ps.scheduled_at,
                    ps.target_upload_at,
                    ps.target_public_at,
                    ps.date_key,
                    ps.upload_window_start,
                    ps.upload_window_end,
                    ps.slot_position,
                    ps.source_mode,
                    ch.name as channel_name,
                    ch.slug as channel_slug
                   FROM planned_slots ps
                   JOIN channels ch ON ch.id = ps.channel_id
                    WHERE ps.date_key >= date('now', 'localtime')
                      AND ps.date_key <= date('now', 'localtime', '+1 day')
                      AND ps.status = 'pending'
                     AND ps.video_id IS NULL
                   ORDER BY ps.scheduled_at ASC""",
            ).fetchall()
            result["planned"] = [dict(r) for r in planned]

            # ── 2. Generating (active pipeline jobs) ──
            generating = conn.execute(
                """SELECT
                    v.id as video_id,
                    v.channel_id,
                    v.status,
                    v.progress,
                    v.progress_phase,
                    v.target_public_at,
                    v.publish_mode,
                    v.created_at,
                    v.generation_started_at,
                    gj.id as job_id,
                    gj.status as job_status,
                    gj.progress as job_progress,
                    gj.phase as job_phase,
                    ch.name as channel_name,
                    ch.slug as channel_slug
                   FROM videos v
                   JOIN channels ch ON ch.id = v.channel_id
                   LEFT JOIN generation_jobs gj ON gj.video_id = v.id AND gj.status = 'running'
                    WHERE v.status = 'generating' OR gj.id IS NOT NULL
                   ORDER BY v.created_at ASC""",
            ).fetchall()
            result["generating"] = [dict(r) for r in generating]

            # ── 3. Awaiting Upload (F1 complete, waiting for F2 upload window) ──
            awaiting = conn.execute(
                """SELECT
                    v.id as video_id,
                    v.channel_id,
                    v.status,
                    v.titulo_final,
                    v.target_public_at,
                    v.scheduled_upload_at,
                    v.publish_mode,
                    v.progress,
                    v.progress_phase,
                    v.created_at,
                    v.generation_finished_at,
                    ch.name as channel_name,
                    ch.slug as channel_slug
                   FROM videos v
                   JOIN channels ch ON ch.id = v.channel_id
                   WHERE v.status IN ('awaiting_upload', 'uploading')
                     AND v.video_path IS NOT NULL
                     AND v.video_path != ''
                   ORDER BY v.created_at ASC""",
            ).fetchall()
            # Add derived target_upload_at from planned_slots if available
            awaiting_list = []
            for r in awaiting:
                d = dict(r)
                # Prefer scheduled_upload_at (set by upload_scheduler), fallback to planned_slot
                if not d.get("scheduled_upload_at"):
                    ps = conn.execute(
                        "SELECT target_upload_at FROM planned_slots WHERE video_id = ? LIMIT 1",
                        (d["video_id"],),
                    ).fetchone()
                    d["target_upload_at"] = ps["target_upload_at"] if ps else None
                else:
                    d["target_upload_at"] = d["scheduled_upload_at"]
                awaiting_list.append(d)
            result["awaiting_upload"] = awaiting_list

            # ── 4. Warming (uploaded private, waiting for go_public) ──
            warming = conn.execute(
                """SELECT
                    v.id as video_id,
                    v.channel_id,
                    v.status,
                    v.privacy_status,
                    v.yt_video_id,
                    v.titulo_final,
                    v.target_public_at,
                    v.uploaded_at,
                    v.publish_mode,
                    v.peak_source,
                    v.auto_playlist_id,
                    v.auto_playlist_name,
                    v.manual_altered_content_done,
                    v.manual_end_screens_done,
                    ch.name as channel_name,
                    ch.slug as channel_slug
                   FROM videos v
                   JOIN channels ch ON ch.id = v.channel_id
                   WHERE v.status = 'uploaded_private'
                     AND v.target_public_at IS NOT NULL
                     AND datetime(v.target_public_at) > datetime('now')
                   ORDER BY v.target_public_at ASC""",
            ).fetchall()
            result["warming"] = [dict(r) for r in warming]

            # ── 5. Shorts pending (slots for today, not dispatched yet) ──
            shorts_pending = conn.execute(
                """SELECT
                    sps.id as slot_id,
                    sps.channel_id,
                    sps.date_key,
                    sps.scheduled_at,
                    sps.target_upload_at,
                    sps.short_type,
                    sps.slot_position,
                    sps.long_slot_position,
                    sps.source_video_id,
                    sps.status,
                    ch.name as channel_name,
                    ch.slug as channel_slug
                   FROM shorts_planned_slots sps
                   JOIN channels ch ON ch.id = sps.channel_id
                    WHERE sps.date_key >= date('now', 'localtime')
                      AND sps.date_key <= date('now', 'localtime', '+1 day')
                      AND sps.status = 'pending'
                    ORDER BY sps.scheduled_at ASC""",
            ).fetchall()
            result["shorts"]["pending"] = [dict(r) for r in shorts_pending]

            # ── 5. Shorts generating (running slots with job progress) ──
            shorts_generating = conn.execute(
                """SELECT
                    sps.id as slot_id,
                    sps.channel_id,
                    sps.date_key,
                    sps.scheduled_at,
                    sps.target_upload_at,
                    sps.short_type,
                    sps.slot_position,
                    sps.long_slot_position,
                    sps.source_video_id,
                    sps.status,
                    sps.job_id,
                    gj.status as job_status,
                    gj.progress as job_progress,
                    gj.phase as job_phase,
                    ch.name as channel_name,
                    ch.slug as channel_slug
                   FROM shorts_planned_slots sps
                   JOIN channels ch ON ch.id = sps.channel_id
                   LEFT JOIN generation_jobs gj ON gj.id = sps.job_id
                   WHERE sps.status = 'running'
                   ORDER BY sps.scheduled_at ASC""",
            ).fetchall()
            result["shorts"]["generating"] = [dict(r) for r in shorts_generating]

            # ── 6. Shorts completed today ──
            shorts_completed = conn.execute(
                """SELECT
                    sps.id as slot_id,
                    sps.channel_id,
                    sps.date_key,
                    sps.scheduled_at,
                    sps.target_upload_at,
                    sps.short_type,
                    sps.slot_position,
                    sps.long_slot_position,
                    sps.source_video_id,
                    sps.status,
                    sps.short_id,
                    s.published_at as actual_completed_at,
                    ch.name as channel_name,
                    ch.slug as channel_slug
                   FROM shorts_planned_slots sps
                   JOIN channels ch ON ch.id = sps.channel_id
                   LEFT JOIN shorts s ON s.id = sps.short_id
                    WHERE sps.date_key >= date('now', 'localtime')
                      AND sps.date_key <= date('now', 'localtime', '+1 day')
                      AND sps.status = 'completed'
                   ORDER BY sps.scheduled_at ASC""",
            ).fetchall()
            result["shorts"]["completed"] = [dict(r) for r in shorts_completed]

            # ── 7. Published in last 24h (both long-form videos & shorts) ──
            published = conn.execute(
                """SELECT id, channel_id, channel_name, channel_slug,
                          title, youtube_id, published_at, content_type
                   FROM (
                       -- Long-form videos
                       SELECT v.id, v.channel_id,
                              ch.name AS channel_name, ch.slug AS channel_slug,
                              v.titulo_final AS title, v.yt_video_id AS youtube_id,
                              v.published_at, 'video' AS content_type
                         FROM videos v
                         JOIN channels ch ON ch.id = v.channel_id
                        WHERE v.privacy_status = 'public'
                          AND v.published_at IS NOT NULL
                          AND v.published_at >= datetime('now', 'localtime', '-1 day')

                        UNION ALL

                       -- Shorts
                       SELECT s.id, s.channel_id,
                              ch2.name, ch2.slug,
                              s.title, s.youtube_id,
                              s.published_at,
                              CASE s.type WHEN 'native' THEN 'native' ELSE 'clip' END
                         FROM shorts s
                         JOIN channels ch2 ON ch2.id = s.channel_id
                        WHERE s.status = 'published'
                          AND s.published_at IS NOT NULL
                          AND s.published_at >= datetime('now', 'localtime', '-1 day')
                   )
                   ORDER BY published_at DESC""",
            ).fetchall()
            result["published_24h"] = [dict(r) for r in published]

        return result

    def get_completed_videos_today(self, channel_id: int) -> list[dict]:
        """Get today's completed videos for a channel, ordered by created_at.
        Used by clip shorts dispatch to find source long videos."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, titulo_final, yt_video_id, created_at
                   FROM videos
                   WHERE channel_id = ?
                     AND date(created_at) = date('now', 'localtime')
                     AND status IN ('uploaded', 'public')
                   ORDER BY created_at ASC""",
                (channel_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── system_state helpers ───────────────────────────────────

    def get_system_state(self, key: str) -> str | None:
        """Read a key from system_state. Returns None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_system_state(self, key: str, value: str) -> None:
        """Upsert a key-value pair into system_state."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO system_state (key, value, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET
                     value = excluded.value,
                     updated_at = excluded.updated_at""",
                (key, value),
            )
            conn.commit()

    # ── video_asset_history helpers (v9 cross-video dedup) ─────

    def insert_asset_history(self, video_id: int, file_path: str,
                             source: str, asset_url: str = "") -> None:
        """Record an asset as used for cross-video deduplication."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO video_asset_history
                   (video_id, file_path, source, asset_url)
                   VALUES (?, ?, ?, ?)""",
                (video_id, file_path, source, asset_url),
            )
            conn.commit()

    def get_all_used_filenames(self) -> set[str]:
        """Return all filenames ever used (for dedup at fetch time)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT file_path FROM video_asset_history"
            ).fetchall()
        return {r["file_path"] for r in rows if r["file_path"]}

    def delete_video_asset_history(self, video_id: int) -> int:
        """Delete all history rows for a video. Returns count deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM video_asset_history WHERE video_id = ?",
                (video_id,),
            )
            conn.commit()
            return cursor.rowcount
