-- schema_v31.sql — A/B Testing Tables for Sequential Title/Thumbnail Optimization
--
-- Protocol:
--   Día 0: Upload with title_v1 + 1 thumbnail (variant 1)
--   Día 2 (+48h): If CTR < threshold, rotate thumbnail OR title (not both)
--   Día 4 (+48h more): Compare CTR_v1 vs CTR_v2, keep winner
--
-- Tables:
--   video_ab_tests        — per-video A/B test state machine
--   title_formula_performance — accumulated learnings per formula type

CREATE TABLE IF NOT EXISTS video_ab_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    yt_video_id TEXT,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    
    -- State machine phase
    phase TEXT NOT NULL DEFAULT 'pending',
    -- 'pending'          — queued, not yet at first check window
    -- 'first_check'      — ready for initial CTR evaluation
    -- 'title_rotated'    — title was changed, waiting for second check
    -- 'thumbnail_rotated' — thumbnail was changed, waiting for second check
    -- 'second_check'     — ready for post-change CTR comparison
    -- 'completed'        — test finished, winner chosen
    -- 'skipped'          — CTR already good, no optimization needed
    -- 'insufficient_data' — not enough impressions after max wait
    
    -- Title variants
    title_v1 TEXT,                -- original title at upload
    title_v2 TEXT,                -- alternative title (generated when CTR low)
    
    -- CTR / performance metrics (before change)
    ctr_v1 REAL,                  -- CTR before any change
    impressions_v1 INTEGER,       -- impressions before any change
    retention_v1 REAL,            -- avg view duration before change
    
    -- CTR / performance metrics (after change)
    ctr_v2 REAL,                  -- CTR after title/thumbnail change
    impressions_v2 INTEGER,       -- impressions after change
    retention_v2 REAL,            -- avg view duration after change
    
    -- Results
    winner_title TEXT,            -- winning title after comparison
    winner_thumbnail_variant INTEGER,  -- winning thumbnail variant (1/2/3)
    
    -- Thumbnail variants
    thumbnail_variant_paths TEXT, -- JSON array of file paths [path1, path2, path3]
    thumbnail_variant_active INTEGER DEFAULT 1,  -- which variant is currently on YT
    
    -- Timestamps
    first_checked_at TEXT,
    title_rotated_at TEXT,
    thumbnail_rotated_at TEXT,
    second_checked_at TEXT,
    completed_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_vab_video ON video_ab_tests(video_id);
CREATE INDEX IF NOT EXISTS idx_vab_phase ON video_ab_tests(phase);
CREATE INDEX IF NOT EXISTS idx_vab_channel ON video_ab_tests(channel_id);
CREATE INDEX IF NOT EXISTS idx_vab_channel_phase ON video_ab_tests(channel_id, phase);


-- Accumulated learnings from completed A/B tests.
-- Each row tracks performance of a specific title formula type
-- (question, curiosity_gap, shock, urgency, list, how_to, statement).
-- This data feeds future title generation to prefer high-performing formulas.
CREATE TABLE IF NOT EXISTS title_formula_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    formula_type TEXT NOT NULL,    -- 'question', 'curiosity_gap', 'list', 'how_to', 'shock', 'urgency', 'statement'
    total_tests INTEGER DEFAULT 0,
    total_wins INTEGER DEFAULT 0,
    avg_ctr_improvement REAL DEFAULT 0,  -- average CTR delta when this formula wins
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tfp_channel_formula ON title_formula_performance(channel_id, formula_type);
