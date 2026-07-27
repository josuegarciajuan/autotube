-- Autotube v14 schema: social media accounts per channel
-- Run AFTER schema_v2.sql (idempotent)

-- ── channel_social_accounts ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS channel_social_accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,          -- 'tiktok', 'twitter', 'instagram', 'facebook', 'reddit'
    username            TEXT NOT NULL,
    encrypted_password  TEXT NOT NULL,          -- Fernet-encrypted (AES-128-GCM)
    enabled             BOOLEAN DEFAULT 1,
    cookies_json        TEXT,                   -- Playwright session cookies (JSON string)
    last_login_at       TIMESTAMP,
    last_error          TEXT,                   -- last error message for diagnostics
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(channel_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_csa_channel ON channel_social_accounts(channel_id);
CREATE INDEX IF NOT EXISTS idx_csa_platform ON channel_social_accounts(channel_id, platform, enabled);

-- ── social_post_log ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS social_post_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id            INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,          -- 'tiktok', 'twitter', etc.
    account_id          INTEGER REFERENCES channel_social_accounts(id),
    lifecycle_action_id INTEGER REFERENCES video_lifecycle_actions(id),
    post_url            TEXT,                   -- URL of the published post
    post_id             TEXT,                   -- platform-specific post ID
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending|publishing|published|failed
    error_message       TEXT,
    retry_count         INTEGER DEFAULT 0,
    caption_text        TEXT,                   -- the generated caption
    clip_path           TEXT,                   -- local path to the generated clip (if applicable)
    published_at        TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_spl_video ON social_post_log(video_id);
CREATE INDEX IF NOT EXISTS idx_spl_channel ON social_post_log(channel_id, platform, status);
