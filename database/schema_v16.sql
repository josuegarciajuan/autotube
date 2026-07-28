-- Autotube v16 schema: short_asset_history (cross-short dedup tracking)
-- Tracks every media asset ever used in native shorts so new shorts
-- can skip previously-used images and videos.
-- Run AFTER schema_v15.sql (idempotent)

CREATE TABLE IF NOT EXISTS short_asset_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id    INTEGER NOT NULL REFERENCES shorts(id) ON DELETE CASCADE,
    channel_id  INTEGER REFERENCES channels(id),
    file_path   TEXT NOT NULL,       -- relative path, e.g. 'output/images/pixabay_photo_6841384.jpg'
    source      TEXT NOT NULL,       -- provider source, e.g. 'pexels_video', 'pixabay_photo', 'unsplash'
    asset_url   TEXT,                -- download URL (may expire)
    used_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_short_asset_filepath ON short_asset_history(file_path);
CREATE INDEX IF NOT EXISTS idx_short_asset_short ON short_asset_history(short_id);
CREATE INDEX IF NOT EXISTS idx_short_asset_channel ON short_asset_history(channel_id);
