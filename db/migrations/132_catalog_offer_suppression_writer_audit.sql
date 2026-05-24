-- 132_catalog_offer_suppression_writer_audit.sql
--
-- PDP / commerce-index repair PR-1.
-- Strictly additive: add a reversible suppression primitive on
-- catalog_offers plus a writer_audit_log table. Existing rows are not
-- mutated by this migration.
--
-- NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction block.

ALTER TABLE IF EXISTS catalog_offers
  ADD COLUMN IF NOT EXISTS suppression_reason TEXT NULL,
  ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_offers_suppressed
  ON catalog_offers (suppressed_at)
  WHERE suppressed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS writer_audit_log (
  id BIGSERIAL PRIMARY KEY,
  writer_name TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  dry_run_report_hash TEXT NULL,
  applied_rows INT NOT NULL DEFAULT 0,
  skipped_rows INT NOT NULL DEFAULT 0,
  reasons JSONB NULL,
  actor TEXT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_writer_audit_writer_time
  ON writer_audit_log (writer_name, applied_at DESC);

-- DOWN migration is checked in at:
-- db/migrations/down/132_catalog_offer_suppression_writer_audit_down.sql
