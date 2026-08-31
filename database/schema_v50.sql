-- Autotube v50: analytics completeness + collection-runs ledger + operator reminders
--
-- Objetivo:
--   1. Que impresiones, CTR, retención y ventana analizada se persistan en CADA
--      recolección manual desde el dashboard (no solo en modo deep).
--   2. Un ledger por canal de cada recolección (stats_collection_runs) para
--      clasificar cobertura: collected / public_only / requires_authorization /
--      quota_limited / failed — nunca ceros falsos.
--   3. Recordatorios operativos (scheduled_reminders) que SOLO alertan: al vencer
--      generan una alerta de sistema visible en Monitor/Alert Center. NUNCA
--      ejecutan acciones de contenido, stats ni configuración en silencio.

-- ── stats_collection_runs: resultado por canal de cada recolección manual ──
CREATE TABLE IF NOT EXISTS stats_collection_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id   INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    started_at   TIMESTAMP,
    finished_at  TIMESTAMP,
    status       TEXT NOT NULL DEFAULT 'collected',
    deep         INTEGER DEFAULT 0,
    use_data_api INTEGER DEFAULT 1,
    result_json  TEXT DEFAULT '{}',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scr_channel ON stats_collection_runs(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_scr_status  ON stats_collection_runs(status, created_at);

-- ── scheduled_reminders: seguimiento operativo que SOLO alerta ──
-- Un recordatorio vencido (due_at <= now, status=pending) es emitido por el loop
-- `reminders` como alerta de sistema `scheduled_reminder_due`. El entity_id es
-- un identificador ESTABLE (nunca NULL) para que el dedup de pipeline_alerts
-- funcione entre reinicios.
CREATE TABLE IF NOT EXISTS scheduled_reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT NOT NULL DEFAULT 'system',
    entity_id     INTEGER NOT NULL,
    title         TEXT NOT NULL,
    message       TEXT,
    alert_type    TEXT NOT NULL DEFAULT 'scheduled_reminder_due',
    due_at        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | alerted | resolved
    alert_id      INTEGER,
    metadata_json TEXT DEFAULT '{}',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at   TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rem_due    ON scheduled_reminders(status, due_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rem_entity ON scheduled_reminders(entity_type, entity_id);
