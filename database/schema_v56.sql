-- Durable, idempotent editorial review checkpoints (read-only audits).
CREATE TABLE IF NOT EXISTS editorial_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    review_kind TEXT NOT NULL,
    review_date TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK(status IN ('scheduled', 'running', 'succeeded', 'failed')),
    due_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    result_json TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_editorial_review_video_kind
    ON editorial_reviews(video_id, review_kind) WHERE video_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_editorial_review_daily
    ON editorial_reviews(channel_id, review_kind, review_date) WHERE video_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_editorial_reviews_due
    ON editorial_reviews(status, due_at);
