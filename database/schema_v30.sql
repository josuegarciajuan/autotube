-- Autotube v30 schema: channel demographics + analytics tracking
-- Run AFTER schema_v29 (idempotent)

-- ── channel_demographics ──────────────────────────────────────
-- Audience demographics (age + gender breakdown) fetched from
-- YouTube Analytics API via the deep collection pipeline.
CREATE TABLE IF NOT EXISTS channel_demographics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    age_group TEXT NOT NULL,
    gender TEXT NOT NULL,
    views_pct REAL NOT NULL DEFAULT 0,
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(channel_id, age_group, gender, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_cd_channel ON channel_demographics(channel_id, fetched_at);
