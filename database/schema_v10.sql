-- Autotube v10 schema: Smart Scheduling v2 foundation
-- Phase pipelining + RAM tracking
-- Run AFTER schema_v9.sql (idempotent)

-- ── pipeline_phase on generation_jobs ──────────────────────────
-- Tracks which phase the worker is in: 'prep' (scrape→media),
-- 'render' (video assembly), 'post' (metadata+upload).
-- Enables phase pipelining: prep of job B can overlap with render of job A.
ALTER TABLE generation_jobs ADD COLUMN pipeline_phase TEXT DEFAULT NULL;

-- ── peak_ram_mb on videos ──────────────────────────────────────
-- Stores the peak system RAM usage (in MB) measured during the video
-- render phase. Used to calibrate capacity estimation for future runs.
ALTER TABLE videos ADD COLUMN peak_ram_mb INTEGER DEFAULT NULL;

-- ── Index for fast render-phase job lookups ─────────────────────
CREATE INDEX IF NOT EXISTS idx_jobs_pipeline_phase ON generation_jobs(pipeline_phase, status);
