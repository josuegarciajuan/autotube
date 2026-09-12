-- Review governance ledger. Reviews are observational/manual only.
CREATE TABLE IF NOT EXISTS video_review_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    review_kind TEXT NOT NULL,
    due_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    completed_at TIMESTAMP,
    result_json TEXT DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(video_id, review_kind)
);
CREATE INDEX IF NOT EXISTS idx_video_review_tasks_due
    ON video_review_tasks(status, due_at);
CREATE INDEX IF NOT EXISTS idx_video_review_tasks_channel
    ON video_review_tasks(channel_id, due_at);
