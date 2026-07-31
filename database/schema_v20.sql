-- v20: channel_insights — AI self-optimization analysis results
-- Stores full multi-pass LLM analysis with recommendations per channel.
-- Frontend polls this table for real-time progress during analysis.

CREATE TABLE IF NOT EXISTS channel_insights (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'processing',
    current_phase       TEXT,                  -- exploration / hypothesis_recommendations / done
    phase_detail        TEXT,                  -- sub-phase description for frontend feedback
    insights_json       TEXT DEFAULT '{}',     -- final structured analysis (JSON)
    raw_patterns        TEXT,                  -- Phase 1 output (patterns JSON)
    raw_hypotheses      TEXT,                  -- Intermediate hypotheses (legacy; merged into Phase 2)
    error_msg           TEXT,
    model_used          TEXT,
    tokens_input        INTEGER DEFAULT 0,
    tokens_output       INTEGER DEFAULT 0,
    generation_time_ms  INTEGER DEFAULT 0,
    retry_count         INTEGER DEFAULT 0,     -- number of retry attempts (0 = first try)
    heartbeat_at        TIMESTAMP,             -- updated every 15s while analysis is alive
    generated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_at          TIMESTAMP,
    applied_by          TEXT
);

CREATE INDEX IF NOT EXISTS idx_ci_channel ON channel_insights(channel_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ci_status ON channel_insights(channel_id, status);
CREATE INDEX IF NOT EXISTS idx_ci_heartbeat ON channel_insights(heartbeat_at);
