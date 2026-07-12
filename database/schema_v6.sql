-- ── v6: Viral Mirror support ────────────────────────────────────

-- Viral metadata columns for raw_content (YouTube viral videos as source)
ALTER TABLE raw_content ADD COLUMN source_mode TEXT DEFAULT 'original';
ALTER TABLE raw_content ADD COLUMN viral_original_title TEXT;
ALTER TABLE raw_content ADD COLUMN viral_original_description TEXT;
ALTER TABLE raw_content ADD COLUMN viral_original_thumbnail_url TEXT;
ALTER TABLE raw_content ADD COLUMN viral_original_video_url TEXT;
ALTER TABLE raw_content ADD COLUMN viral_views INTEGER DEFAULT 0;
ALTER TABLE raw_content ADD COLUMN viral_upload_date TEXT;
ALTER TABLE raw_content ADD COLUMN viral_duration_sec INTEGER DEFAULT 0;
ALTER TABLE raw_content ADD COLUMN viral_channel_name TEXT;
ALTER TABLE raw_content ADD COLUMN viral_score REAL DEFAULT 0.0;
ALTER TABLE raw_content ADD COLUMN viral_script_es TEXT;
ALTER TABLE raw_content ADD COLUMN viral_meta_json TEXT;

-- Index for viral source lookups
CREATE INDEX IF NOT EXISTS idx_raw_source_mode ON raw_content(source_mode);
CREATE INDEX IF NOT EXISTS idx_raw_viral_score ON raw_content(viral_score);

-- Source mode for planned slots (so each slot can choose original/viral method)
ALTER TABLE planned_slots ADD COLUMN source_mode TEXT DEFAULT 'original';

-- Source mode for shorts planned slots (future-proofing)
ALTER TABLE shorts_planned_slots ADD COLUMN source_mode TEXT DEFAULT 'original';
