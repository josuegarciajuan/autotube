-- Autotube v4 schema: YouTube Shorts support
-- Run AFTER schema_v3.sql (idempotent)

-- ── shorts ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shorts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    source_video_id INTEGER,              -- NULL for native shorts
    type TEXT NOT NULL DEFAULT 'clip',    -- 'clip' | 'native'
    title TEXT,                           -- SEO title (max 100 chars)
    hook_title TEXT,                      -- Short hook title (shown in card)
    hook_text TEXT,                       -- Hook text burned on screen
    start_time REAL,                      -- Timecode in source video (seconds, clips only)
    end_time REAL,                        -- Timecode in source video (seconds, clips only)
    duration REAL,                        -- Final duration in seconds
    status TEXT NOT NULL DEFAULT 'pending', -- pending|extracted|rendering|ready|uploading|published|failed
    scheduled_date TEXT,                  -- YYYY-MM-DD scheduled publish date
    published_at TEXT,                    -- Actual publish timestamp
    youtube_id TEXT,                      -- YouTube video ID after upload
    youtube_url TEXT,                     -- Full YouTube URL
    file_path TEXT,                       -- Rendered .mp4 file path
    thumbnail_path TEXT,                  -- Thumbnail path
    ranking INTEGER,                      -- 1-5, impact rank (1 = most impactful)
    error_message TEXT,                   -- Error details if failed
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    FOREIGN KEY (source_video_id) REFERENCES videos(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_shorts_channel ON shorts(channel_id, status);
CREATE INDEX IF NOT EXISTS idx_shorts_source ON shorts(source_video_id);
CREATE INDEX IF NOT EXISTS idx_shorts_scheduled ON shorts(scheduled_date, status);
CREATE INDEX IF NOT EXISTS idx_shorts_youtube ON shorts(youtube_id);

-- ── shorts_schedule ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shorts_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    schedule_date TEXT NOT NULL,          -- YYYY-MM-DD
    target_count INTEGER DEFAULT 1,       -- How many shorts to produce that day
    produced_count INTEGER DEFAULT 0,     -- How many actually produced
    target_time TEXT,                     -- HH:MM target hour for staggered publication
    status TEXT NOT NULL DEFAULT 'pending', -- pending|completed|skipped
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    UNIQUE(channel_id, schedule_date)
);

CREATE INDEX IF NOT EXISTS idx_ss_date ON shorts_schedule(schedule_date, status);
CREATE INDEX IF NOT EXISTS idx_ss_channel ON shorts_schedule(channel_id, schedule_date);

-- ── shorts_planning_config ──────────────────────────────────
CREATE TABLE IF NOT EXISTS shorts_planning_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL UNIQUE,
    shorts_per_day INTEGER DEFAULT 3,
    shorts_enabled BOOLEAN DEFAULT 1,
    shorts_clip_enabled BOOLEAN DEFAULT 1,
    shorts_max_clips INTEGER DEFAULT 5,
    shorts_clip_schedule_json TEXT DEFAULT '[{"offset_days": 1, "count": 1}, {"offset_days": 3, "count": 1}, {"offset_days": 5, "count": 1}]',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);
