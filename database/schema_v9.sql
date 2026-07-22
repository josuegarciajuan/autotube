-- Autotube v9 schema: cross-video asset dedup tracking
-- Tracks every media asset ever used so new videos can skip them
-- even after original files are deleted.

CREATE TABLE IF NOT EXISTS video_asset_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    file_path     TEXT NOT NULL,       -- relative path, e.g. 'output/video_clips/pexels_abc123.mp4'
    source        TEXT NOT NULL,       -- provider source, e.g. 'pexels_video', 'pixabay_photo', 'pollo_ai'
    asset_url     TEXT,                -- download URL (may expire)
    used_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_asset_filepath ON video_asset_history(file_path);
CREATE INDEX IF NOT EXISTS idx_asset_video ON video_asset_history(video_id);
CREATE INDEX IF NOT EXISTS idx_asset_source ON video_asset_history(source);
