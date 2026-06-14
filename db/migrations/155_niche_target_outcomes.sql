-- 155_niche_target_outcomes.sql
-- Phase 4 (measure & defend): per-merchant per-niche ownership over time, so a
-- re-audit can show whether the merchant WON the niches Phase 2 told them to
-- target. One row per (merchant, niche query, audit run) — a history. Comparing
-- the two most-recent audits of a query yields the movement:
--   open-lane -> merchant-owned  = WON
--   merchant-owned -> competitor = LOST / contested (defend)
--   open -> open                 = still open (keep working)
-- This is the loop-closing signal: audit -> strategy -> create -> MEASURE ->
-- re-audit. It compounds as audits re-run; aggregated_outcomes (migration 149)
-- carries the orthogonal TRANSACTION outcomes.
--
-- Idempotent. Prod skips the migration runner — apply via railway ssh / admin.
BEGIN;

CREATE TABLE IF NOT EXISTS niche_target_outcomes (
    id                BIGSERIAL PRIMARY KEY,
    merchant_id       TEXT NOT NULL,
    normalized_query  TEXT NOT NULL,
    ownership_state   TEXT,
    opportunity_score NUMERIC(6, 2),
    open_lane         BOOLEAN NOT NULL DEFAULT FALSE,
    audit_run_id      TEXT,
    seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The movement read: latest rows per (merchant, query).
CREATE INDEX IF NOT EXISTS idx_niche_target_outcomes_lookup
  ON niche_target_outcomes (merchant_id, normalized_query, seen_at DESC);

COMMIT;
