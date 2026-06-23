-- Autotube v2 schema extensions
-- Run AFTER schema.sql

-- ── channels ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL DEFAULT '{}',
    active      BOOLEAN NOT NULL DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_channels_slug ON channels(slug);
CREATE INDEX IF NOT EXISTS idx_channels_active ON channels(active);

-- ── videos extensions ─────────────────────────────────────
-- Add columns if they don't exist (SQLite doesn't support IF NOT EXISTS for ALTER)
-- We use a migration approach: try to add, ignore if exists
-- (handled in Python migration code)

-- ── video_scenes ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS video_scenes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    scene_order   INTEGER NOT NULL,
    description   TEXT,
    script_text   TEXT,
    audio_path    TEXT,
    image_path    TEXT,
    image_url     TEXT,
    subtitle_text TEXT,
    duration_ms   INTEGER,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scenes_video ON video_scenes(video_id, scene_order);

-- ── generation_jobs ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS generation_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    video_id    INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    action      TEXT NOT NULL DEFAULT 'generate',
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    INTEGER DEFAULT 0,
    phase       TEXT,
    error_msg   TEXT,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_channel ON generation_jobs(channel_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON generation_jobs(status);
