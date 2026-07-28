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
    topic TEXT,                           -- Short topic/theme (for dedup across native shorts)
    has_subscribe_cta BOOLEAN DEFAULT 0,  -- Whether this short includes a subscribe CTA block
    longform_linked BOOLEAN DEFAULT 0,    -- Whether YouTube Studio "Related video" is linked to source video
    longform_linked_at TEXT,              -- When the link was set (YYYY-MM-DD HH:MM:SS)
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
-- v4.1: cleaner fields — shorts_native_per_day + shorts_clip_per_day
-- Old columns (shorts_per_day, shorts_clip_enabled, shorts_max_clips,
-- shorts_clip_schedule_json) are deprecated and migrated away in db_extended.py.
CREATE TABLE IF NOT EXISTS shorts_planning_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL UNIQUE,
    shorts_native_per_day INTEGER DEFAULT 5,
    shorts_clip_per_day INTEGER DEFAULT 2,
    shorts_enabled BOOLEAN DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

-- ── shorts_planned_slots ─────────────────────────────────────
-- Individual slot scheduling for shorts. Replaces the legacy
-- shorts_schedule table (which is kept but no longer used).
CREATE TABLE IF NOT EXISTS shorts_planned_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    date_key TEXT NOT NULL,                  -- YYYY-MM-DD
    scheduled_at TIMESTAMP NOT NULL,         -- when to start generation
    target_upload_at TIMESTAMP,              -- target YouTube publish time
    short_type TEXT NOT NULL DEFAULT 'native', -- 'native' | 'clip'
    long_slot_position INTEGER,              -- for clips: which long-form slot this pairs with
    source_video_id INTEGER,                 -- for clips: source long-form video
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|running|completed|failed|cancelled|skipped
    job_id INTEGER,                          -- FK → generation_jobs
    short_id INTEGER,                        -- FK → shorts
    slot_position INTEGER DEFAULT 0,         -- order within the day
    error_message TEXT,                      -- error details if failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    FOREIGN KEY (source_video_id) REFERENCES videos(id),
    FOREIGN KEY (short_id) REFERENCES shorts(id)
);

CREATE INDEX IF NOT EXISTS idx_sps_date ON shorts_planned_slots(date_key, status);
CREATE INDEX IF NOT EXISTS idx_sps_channel ON shorts_planned_slots(channel_id, date_key);
CREATE INDEX IF NOT EXISTS idx_sps_status ON shorts_planned_slots(status, scheduled_at);
