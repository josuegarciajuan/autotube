-- Durable recovery experiment checkpoints. One row per video/checkpoint.
CREATE TABLE IF NOT EXISTS recovery_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    channel_id INTEGER,
    checkpoint_hours INTEGER NOT NULL,
    executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(video_id, checkpoint_hours)
);
CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_video
    ON recovery_checkpoints(video_id, checkpoint_hours);
