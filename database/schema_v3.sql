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
    fetched_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vsh_video ON video_stats_history(video_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_vsh_ytid ON video_stats_history(yt_video_id, fetched_at);

-- ── channel_stats_history ───────────────────────────────────
CREATE TABLE IF NOT EXISTS channel_stats_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    subscribers   INTEGER DEFAULT 0,
    total_views   INTEGER DEFAULT 0,
    video_count   INTEGER DEFAULT 0,
    fetched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_csh_channel ON channel_stats_history(channel_id, fetched_at);
