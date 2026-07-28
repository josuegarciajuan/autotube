-- Autotube v17 schema: lifecycle events + pipeline alerts
-- Unified monitoring system for videos AND shorts
-- Run AFTER schema_v16 (idempotent)

-- ── lifecycle_events: audit trail for every entity ─────────────
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL CHECK(entity_type IN ('video', 'short')),
    entity_id       INTEGER NOT NULL,
    channel_id      INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    event           TEXT NOT NULL,          -- phase_started, phase_completed, phase_failed,
                                            -- upload_started, upload_completed, upload_failed,
                                            -- publish_scheduled, publish_completed, publish_failed,
                                            -- generation_started, generation_completed, generation_failed,
                                            -- render_started, render_completed, render_failed
    phase           TEXT,                   -- scrape, script, tts, media, video, metadata, upload, render (for shorts)
    status          TEXT NOT NULL DEFAULT 'started',  -- started, completed, failed, warning, info
    message         TEXT,                   -- human-readable description
    metadata_json   TEXT,                   -- JSON: duration_ms, yt_video_id, error_traceback, file_path, etc.
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_entity ON lifecycle_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_channel ON lifecycle_events(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_event ON lifecycle_events(event, created_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_time ON lifecycle_events(created_at);

-- ── pipeline_alerts: proactive anomaly detection ──────────────
CREATE TABLE IF NOT EXISTS pipeline_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL CHECK(entity_type IN ('video', 'short', 'system')),
    entity_id       INTEGER,               -- videos.id or shorts.id (NULL for system alerts)
    channel_id      INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    alert_type      TEXT NOT NULL,          -- stuck, timeout, failed, crash, orphan, stall, publish_delayed
    severity        TEXT NOT NULL DEFAULT 'warning',  -- critical, warning, info
    title           TEXT NOT NULL,          -- Short alert title
    message         TEXT,                   -- Detailed description
    metadata_json   TEXT,                   -- JSON: duration_stuck_s, phase, last_heartbeat, yt_video_id, etc.
    acknowledged    BOOLEAN DEFAULT 0,      -- User has seen it
    resolved        BOOLEAN DEFAULT 0,      -- Issue is fixed (auto or manual)
    resolved_at     TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_active ON pipeline_alerts(acknowledged, resolved, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_entity ON pipeline_alerts(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON pipeline_alerts(severity, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_channel ON pipeline_alerts(channel_id, created_at);
