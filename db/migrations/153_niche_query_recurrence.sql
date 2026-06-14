-- 153_niche_query_recurrence.sql
-- Cross-merchant niche-demand signal (Phase 2 v2 operator-chosen demand-proxy).
--
-- One row per (normalized niche query, merchant). How many DISTINCT merchants a
-- niche query recurs across is a PROPRIETARY, COMPOUNDING demand proxy: a niche
-- that keeps showing up as Pivota probes more brands is far more likely to have
-- real shopper demand than one seen once — and no competitor has Pivota's
-- cross-brand audit history. Populated on each per-SKU audit from the probed
-- sidewalk (niche) queries; read by build_where_you_can_win to let the operator
-- rank winnable niches by recurrence instead of single-probe demand.
--
-- Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Prod skips the migration runner,
-- so apply via the admin route / psql (see routes/admin_run_migration_*).
BEGIN;

CREATE TABLE IF NOT EXISTS niche_query_recurrence (
    normalized_query  TEXT NOT NULL,
    merchant_id       TEXT NOT NULL,
    runs              INTEGER NOT NULL DEFAULT 1,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (normalized_query, merchant_id)
);

-- The read path aggregates by normalized_query across merchants.
CREATE INDEX IF NOT EXISTS idx_niche_query_recurrence_query
  ON niche_query_recurrence (normalized_query);

COMMIT;
