-- Autotube database schema
-- SQLite with WAL mode for concurrent read/write

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── raw_content: scraped source material ──────────────────────
CREATE TABLE IF NOT EXISTS raw_content (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,              -- 'reddit', 'wikipedia'
    subreddit   TEXT,                       -- Reddit subreddit name (if applicable)
    url         TEXT NOT NULL UNIQUE,       -- canonical source URL
    title       TEXT NOT NULL,
    text        TEXT NOT NULL,              -- full content text
    score       INTEGER DEFAULT 0,          -- upvotes / relevance
    scraped_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used        BOOLEAN DEFAULT 0,          -- already processed?
    canal       TEXT DEFAULT 'canal1'
);

CREATE INDEX IF NOT EXISTS idx_raw_unused ON raw_content(canal, used, scraped_at);
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_content(source);
CREATE INDEX IF NOT EXISTS idx_raw_url ON raw_content(url);

-- ── scripts: GPT-generated scripts ────────────────────────────
CREATE TABLE IF NOT EXISTS scripts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_content_id      INTEGER REFERENCES raw_content(id),
    canal               TEXT NOT NULL,
    titulo_options      TEXT NOT NULL,      -- JSON array of 3 options
    titulo_selected     TEXT,               -- the one we picked
    guion               TEXT NOT NULL,      -- full text with [ESCENA:...] markers
    escenas_json        TEXT NOT NULL,      -- JSON array of scene descriptions
    bloques_json        TEXT,               -- JSON array of block objects (v2)
    emociones_json      TEXT,               -- JSON array of {parrafo, emocion}
    keywords_json       TEXT,               -- JSON array of keywords
    duracion_estimada   INTEGER,            -- estimated minutes
    token_count         INTEGER,            -- GPT tokens used
    cost_estimate       REAL,               -- estimated cost in USD
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used                BOOLEAN DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_scripts_unused ON scripts(canal, used, created_at);
CREATE INDEX IF NOT EXISTS idx_scripts_content ON scripts(raw_content_id);

-- ── videos: tracking uploaded videos ──────────────────────────
CREATE TABLE IF NOT EXISTS videos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id       INTEGER REFERENCES scripts(id),
    canal           TEXT NOT NULL,
    video_path      TEXT NOT NULL,          -- local path
    thumbnail_path  TEXT,                   -- local thumbnail path
    audio_path      TEXT,                   -- TTS audio path
    yt_video_id     TEXT,                   -- YouTube video ID
    yt_url          TEXT,                   -- full YouTube URL
    titulo_final    TEXT,                   -- final published title
    duracion_seg    INTEGER,                -- actual video duration in seconds
    privacy_status  TEXT DEFAULT 'unlisted',
    uploaded_at     TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_videos_canal ON videos(canal, created_at);
CREATE INDEX IF NOT EXISTS idx_videos_script ON videos(script_id);
CREATE INDEX IF NOT EXISTS idx_videos_yt ON videos(yt_video_id);

-- ── pipeline_log: execution history ───────────────────────────
CREATE TABLE IF NOT EXISTS pipeline_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canal       TEXT NOT NULL,
    phase       TEXT NOT NULL,              -- 'scrape', 'script', 'tts', 'images', 'video', 'upload'
    status      TEXT NOT NULL,              -- 'success', 'error', 'skipped'
    message     TEXT,
    content_id  INTEGER,                    -- raw_content.id or scripts.id
    duration_ms INTEGER,                    -- execution time in ms
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_plog_canal ON pipeline_log(canal, created_at);
CREATE INDEX IF NOT EXISTS idx_plog_phase ON pipeline_log(phase);
