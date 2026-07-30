-- Autotube schema v21: script generation attempt tracking + emergency mode
-- Migración idempotente — usa IF NOT EXISTS

-- ── script_generation_attempts: structured failure logging ──────
-- Registra cada intento de generación (por modelo, por reintento)
-- para análisis de patrones de fallo y optimización del pool.
CREATE TABLE IF NOT EXISTS script_generation_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        INTEGER,                    -- FK a videos (NULL si generación batch fuera de pipeline)
    content_id      INTEGER,                    -- FK a raw_content
    canal           TEXT NOT NULL,
    model_name      TEXT NOT NULL,              -- "deepseek-v4-pro", "gpt-4o-mini", etc.
    attempt_number  INTEGER NOT NULL DEFAULT 1, -- 1-based dentro de ese modelo
    pool_position   INTEGER NOT NULL DEFAULT 0, -- posición del modelo en el pool (0-based)
    success         BOOLEAN NOT NULL DEFAULT 0,
    error_type      TEXT,                       -- "json_parse", "empty_content", "timeout", "rate_limit", "validation_failed", "exception"
    error_message   TEXT,                       -- mensaje de error textual (truncado a 500 chars)
    phase           TEXT NOT NULL DEFAULT 'blocks', -- "outline", "blocks", "enrich", "metadata", "quality_check"
    tokens_input    INTEGER,
    tokens_output   INTEGER,
    cost_estimate   REAL,
    duration_ms     INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sga_canal ON script_generation_attempts(canal, created_at);
CREATE INDEX IF NOT EXISTS idx_sga_video ON script_generation_attempts(video_id);
CREATE INDEX IF NOT EXISTS idx_sga_success ON script_generation_attempts(success, created_at);

-- ── scripts: add emergency_mode flag ───────────────────────────
-- Marca guiones generados en modo emergencia (todos los modelos fallaron).
-- Estos guiones tienen menor control de calidad pero aseguran la publicación.
ALTER TABLE scripts ADD COLUMN emergency_mode BOOLEAN NOT NULL DEFAULT 0;
