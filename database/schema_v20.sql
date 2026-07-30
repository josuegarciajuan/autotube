-- v20: channel_insights — AI self-optimization analysis results
-- Stores full multi-pass LLM analysis with recommendations per channel.
-- Frontend polls this table for real-time progress during analysis.

CREATE TABLE IF NOT EXISTS channel_insights (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'processing',
    current_phase       TEXT,                  -- exploration / hypothesis / recommendations
    insights_json       TEXT DEFAULT '{}',     -- final structured analysis (JSON)
    raw_patterns        TEXT,                  -- Phase 1 output (JSON)
    raw_hypotheses      TEXT,                  -- Phase 2 output (JSON)
    error_msg           TEXT,
    model_used          TEXT,
    tokens_input        INTEGER DEFAULT 0,
    tokens_output       INTEGER DEFAULT 0,
    generation_time_ms  INTEGER DEFAULT 0,
    generated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_at          TIMESTAMP,
    applied_by          TEXT
);

CREATE INDEX IF NOT EXISTS idx_ci_channel ON channel_insights(channel_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ci_status ON channel_insights(channel_id, status);
