-- Autotube v15 schema: shorts_planned_slots slot_rank tracking
-- Run AFTER schema_v4.sql (idempotent)

-- Add slot_rank column to shorts_planned_slots to track
-- which optimal_publish_slot was used for this short.
-- Enables performance feedback (record_slot_result) for shorts.
ALTER TABLE shorts_planned_slots ADD COLUMN slot_rank INTEGER DEFAULT 0;
