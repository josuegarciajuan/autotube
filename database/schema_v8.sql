-- Autotube v8 schema: gamification + events + heatmap support for dashboard v3
-- Run AFTER schema_v7 (idempotent)

-- ── streaks: rachas de actividad por canal ───────────────────
CREATE TABLE IF NOT EXISTS streaks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    streak_type     TEXT NOT NULL CHECK(streak_type IN (
                        'daily_views_above_average',
                        'daily_publications',
                        'daily_subs_growth'
                    )),
    current_count   INTEGER DEFAULT 0,
    longest         INTEGER DEFAULT 0,
    last_date       TEXT,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, streak_type)
);

CREATE INDEX IF NOT EXISTS idx_streaks_channel ON streaks(channel_id);

-- ── badges: logros desbloqueados por canal ────────────────────
CREATE TABLE IF NOT EXISTS badges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    badge_key       TEXT NOT NULL,
    unlocked_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, badge_key)
);

CREATE INDEX IF NOT EXISTS idx_badges_channel ON badges(channel_id);

-- ── system_events: log de eventos del sistema ─────────────────
CREATE TABLE IF NOT EXISTS system_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    event_type      TEXT NOT NULL,
    message         TEXT NOT NULL,
    metadata_json   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sysev_time ON system_events(created_at);
CREATE INDEX IF NOT EXISTS idx_sysev_channel ON system_events(channel_id);
