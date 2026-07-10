-- Autotube v5 schema: Video Lifecycle & Promotion System
-- Run AFTER schema_v4.sql (idempotent)

-- ═══════════════════════════════════════════════════════════════
-- Playlists cacheadas de YouTube
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS youtube_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    slug TEXT NOT NULL,                   -- ej: "milagros-modernos"
    yt_playlist_id TEXT NOT NULL,         -- ID de YouTube (PLxxx...)
    name TEXT,                            -- Nombre en YouTube
    playlist_type TEXT DEFAULT 'thematic', -- main | onboarding | thematic
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (channel_id) REFERENCES channels(id),
    UNIQUE(channel_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_yp_channel ON youtube_playlists(channel_id);
CREATE INDEX IF NOT EXISTS idx_yp_slug ON youtube_playlists(channel_id, slug);

-- ═══════════════════════════════════════════════════════════════
-- Asignaciones video → playlist
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS video_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    playlist_id INTEGER NOT NULL,         -- FK → youtube_playlists.id
    yt_playlist_item_id TEXT,             -- ID del item en YouTube
    added_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (video_id) REFERENCES videos(id),
    FOREIGN KEY (playlist_id) REFERENCES youtube_playlists(id),
    UNIQUE(video_id, playlist_id)
);

CREATE INDEX IF NOT EXISTS idx_vp_video ON video_playlists(video_id);

-- ═══════════════════════════════════════════════════════════════
-- Timeline de acciones del video lifecycle
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS video_lifecycle_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    -- Tipos: playlist_add | first_comment | comment_reply_1 |
    --        comment_reply_2 | ctr_check | metadata_reoptimize
    channel_id INTEGER NOT NULL,
    yt_video_id TEXT,
    scheduled_for TEXT NOT NULL,           -- ISO8601 timestamp
    executed_at TEXT,
    status TEXT DEFAULT 'pending',         -- pending | executed | failed | skipped | cancelled
    config_json TEXT,                      -- Parámetros de la acción (JSON)
    result_json TEXT,                      -- Resultado (JSON)
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (video_id) REFERENCES videos(id),
    FOREIGN KEY (channel_id) REFERENCES channels(id)
);

CREATE INDEX IF NOT EXISTS idx_vla_due ON video_lifecycle_actions(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_vla_video ON video_lifecycle_actions(video_id, action_type);

-- ═══════════════════════════════════════════════════════════════
-- Registro de comentarios publicados (evitar duplicados)
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS comment_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    yt_video_id TEXT NOT NULL,
    yt_comment_id TEXT NOT NULL,           -- ID del comentario en YouTube
    parent_comment_id TEXT,                -- NULL = top-level, valor = reply a ese ID
    comment_type TEXT DEFAULT 'first',     -- first | reply
    comment_text TEXT,
    is_pinned BOOLEAN DEFAULT 0,          -- Manual (no automático vía API)
    posted_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (video_id) REFERENCES videos(id)
);

CREATE INDEX IF NOT EXISTS idx_cl_video ON comment_log(video_id, comment_type);
