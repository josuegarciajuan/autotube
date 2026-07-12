-- Autotube v3 schema: YouTube stats history
-- Run AFTER schema_v2.sql (idempotent)

-- ── video_stats_history ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS video_stats_history (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id                   INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    yt_video_id                TEXT NOT NULL,
    views                      INTEGER DEFAULT 0,
    likes                      INTEGER DEFAULT 0,
    comments                   INTEGER DEFAULT 0,
    estimated_minutes_watched  REAL DEFAULT 0,
    average_view_duration      REAL DEFAULT 0,
    subscribers_gained         INTEGER DEFAULT 0,
    estimated_revenue_min      REAL DEFAULT 0,
    estimated_revenue_max      REAL DEFAULT 0,
    embeddable                 INTEGER DEFAULT 1,
    fetched_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vsh_video ON video_stats_history(video_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_vsh_ytid ON video_stats_history(yt_video_id, fetched_at);

-- ── channel_stats_history ───────────────────────────────────
CREATE TABLE IF NOT EXISTS channel_stats_history (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id                INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    subscribers               INTEGER DEFAULT 0,
    total_views               INTEGER DEFAULT 0,
    video_count               INTEGER DEFAULT 0,
    estimated_minutes_watched  REAL DEFAULT 0,
    estimated_revenue_min      REAL DEFAULT 0,
    estimated_revenue_max      REAL DEFAULT 0,
    fetched_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_csh_channel ON channel_stats_history(channel_id, fetched_at);

-- ── planned_slots ──────────────────────────────────────────────
-- Dynamic daily video planning: computed slots for each channel.
-- Replaces the old interval-based content_schedules approach.
CREATE TABLE IF NOT EXISTS planned_slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    date_key        TEXT NOT NULL,               -- YYYY-MM-DD
    scheduled_at    TIMESTAMP NOT NULL,          -- generation start time (UTC)
    target_upload_at TIMESTAMP,                  -- target YouTube publish time (future: publishAt)
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|running|completed|cancelled|skipped
    job_id          INTEGER REFERENCES generation_jobs(id) ON DELETE SET NULL,
    video_id        INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    slot_position   INTEGER DEFAULT 0,           -- 1,2,3... order within the day
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ps_date ON planned_slots(date_key, status);
CREATE INDEX IF NOT EXISTS idx_ps_channel ON planned_slots(channel_id, date_key);

-- ── channel_templates ──────────────────────────────────────────
-- Pre-generated intro/CTA/outro mini-videos cached per channel.
CREATE TABLE IF NOT EXISTS channel_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    segment_type TEXT NOT NULL CHECK(segment_type IN ('intro', 'cta', 'outro')),
    video_path TEXT,
    image_path TEXT,
    config_json TEXT,  -- JSON with generation params
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    UNIQUE(channel_id, segment_type)
);
