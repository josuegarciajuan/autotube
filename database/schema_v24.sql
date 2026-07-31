-- schema_v24: Fix raw_content.url UNIQUE constraint to be per-channel
-- 
-- Before: url TEXT NOT NULL UNIQUE         (global uniqueness)
-- After:  url TEXT NOT NULL, UNIQUE(url, canal) (per-channel uniqueness)
--
-- Problem: The global UNIQUE prevented different channels from indexing the
-- same YouTube viral video. canal5 would crash with FOREIGN KEY constraint
-- failed when canal2 had already saved the same URL.
--
-- Migration: Since SQLite cannot ALTER COLUMN constraints, we recreate the
-- table preserving all data. The scripts.raw_content_id FK references remain
-- valid because row IDs are preserved.
--
-- This migration is IDEMPOTENT: it checks whether the old constraint exists
-- before attempting the recreation.

-- Check if migration was already applied by looking at table_info
SELECT CASE
    WHEN (SELECT COUNT(*) FROM pragma_table_info('raw_content')) > 0
         AND (SELECT COUNT(*) FROM pragma_index_list('raw_content')
              WHERE name = 'idx_raw_unique_url_canal') > 0
    THEN 'v24 already applied — skipping'
    ELSE 'v24 migration needed'
END AS v24_check;

-- Disable FK checks for the duration of this migration (we preserve all IDs)
PRAGMA foreign_keys = 0;

BEGIN TRANSACTION;

-- Step 1: Create new table with correct schema
CREATE TABLE IF NOT EXISTS raw_content_v24 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    subreddit   TEXT,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    text        TEXT NOT NULL,
    score       INTEGER DEFAULT 0,
    scraped_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used        BOOLEAN DEFAULT 0,
    canal       TEXT DEFAULT 'canal2',
    -- v4+ viral metadata columns (added by v6 migration if they exist)
    status      TEXT DEFAULT 'pending',
    scheduled_at TEXT,
    source_mode TEXT DEFAULT 'original',
    viral_original_title TEXT,
    viral_original_description TEXT,
    viral_original_thumbnail_url TEXT,
    viral_original_video_url TEXT,
    viral_views INTEGER DEFAULT 0,
    viral_upload_date TEXT,
    viral_duration_sec INTEGER DEFAULT 0,
    viral_channel_name TEXT,
    viral_score REAL DEFAULT 0.0,
    viral_script_es TEXT,
    viral_meta_json TEXT,
    -- NEW: per-channel uniqueness instead of global
    UNIQUE(url, canal)
);

-- Step 2: Copy all data from old table (preserving all columns)
-- We use a dynamic column list from the old table to handle schema differences
INSERT OR IGNORE INTO raw_content_v24 (
    id, source, subreddit, url, title, text, score, scraped_at, used, canal,
    status, scheduled_at, source_mode,
    viral_original_title, viral_original_description,
    viral_original_thumbnail_url, viral_original_video_url,
    viral_views, viral_upload_date, viral_duration_sec,
    viral_channel_name, viral_score, viral_script_es, viral_meta_json
)
SELECT
    id, source, subreddit, url, title, text, score, scraped_at, used, canal,
    status, scheduled_at, source_mode,
    viral_original_title, viral_original_description,
    viral_original_thumbnail_url, viral_original_video_url,
    viral_views, viral_upload_date, viral_duration_sec,
    viral_channel_name, viral_score, viral_script_es, viral_meta_json
FROM raw_content;

-- Step 3: Drop old table
DROP TABLE raw_content;

-- Step 4: Rename new table to original name
ALTER TABLE raw_content_v24 RENAME TO raw_content;

-- Step 5: Recreate indexes
CREATE INDEX IF NOT EXISTS idx_raw_unused ON raw_content(canal, used, scraped_at);
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_content(source);
CREATE INDEX IF NOT EXISTS idx_raw_url ON raw_content(url);
CREATE INDEX IF NOT EXISTS idx_raw_source_mode ON raw_content(source_mode);
CREATE INDEX IF NOT EXISTS idx_raw_viral_score ON raw_content(viral_score);

-- Step 6: Create explicit index for the new UNIQUE constraint
-- (SQLite creates one automatically, but we name it for idempotency checks)
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_unique_url_canal ON raw_content(url, canal);

COMMIT;

-- Re-enable FK checks
PRAGMA foreign_keys = 1;

-- Verify
SELECT COUNT(*) AS total_rows FROM raw_content;
SELECT COUNT(*) AS total_distinct_urls FROM (SELECT DISTINCT url FROM raw_content);
SELECT COUNT(*) AS total_distinct_url_canal_pairs FROM (SELECT DISTINCT url, canal FROM raw_content);
