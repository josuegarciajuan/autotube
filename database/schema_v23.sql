-- Autotube schema v23: scheduled publish verification columns
-- Migración idempotente — usa IF NOT EXISTS

-- ── videos: published verification tracking ────────────────────
-- published_verified_at: última vez que se verificó el estado vía YouTube API
-- published_retry_at:   próxima verificación si YouTube aún no ha publicado
-- published_retry_count: número de reintentos ya realizados (para backoff exponencial)
ALTER TABLE videos ADD COLUMN published_verified_at TIMESTAMP;
ALTER TABLE videos ADD COLUMN published_retry_at TIMESTAMP;
ALTER TABLE videos ADD COLUMN published_retry_count INTEGER NOT NULL DEFAULT 0;
