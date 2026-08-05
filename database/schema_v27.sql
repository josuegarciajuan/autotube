-- schema_v27.sql: Cross-platform video publishing
-- Tracks full video uploads to non-YouTube platforms (Facebook, Rumble, TikTok)
-- for monetization and traffic diversification.

CREATE TABLE IF NOT EXISTS platform_videos (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id             INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    channel_id           INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    platform             TEXT    NOT NULL,          -- 'facebook', 'rumble', 'tiktok'
    platform_video_id    TEXT,                      -- Platform-specific video ID
    platform_video_url   TEXT,                      -- Public URL
    status               TEXT    NOT NULL DEFAULT 'pending',  -- pending|uploading|processing|published|failed
    privacy              TEXT    DEFAULT 'public',
    error_message        TEXT,
    attempts             INTEGER DEFAULT 0,
    metadata_json        TEXT    DEFAULT '{}',      -- Platform-specific metadata
    uploaded_at          TIMESTAMP,
    last_checked_at      TIMESTAMP,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(video_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_platform_videos_video   ON platform_videos(video_id);
CREATE INDEX IF NOT EXISTS idx_platform_videos_channel ON platform_videos(channel_id, platform);
CREATE INDEX IF NOT EXISTS idx_platform_videos_status  ON platform_videos(status);
