-- Down migration for 132_catalog_offer_suppression_writer_audit.sql.
-- This reverses only the schema additions. It does not mutate or
-- restore any catalog data rows.
--
-- NOTE: DROP INDEX CONCURRENTLY cannot run inside a transaction block.

DROP INDEX CONCURRENTLY IF EXISTS idx_catalog_offers_suppressed;

DROP INDEX IF EXISTS idx_writer_audit_writer_time;
DROP TABLE IF EXISTS writer_audit_log;

ALTER TABLE IF EXISTS catalog_offers
  DROP COLUMN IF EXISTS suppressed_at,
  DROP COLUMN IF EXISTS suppression_reason;
