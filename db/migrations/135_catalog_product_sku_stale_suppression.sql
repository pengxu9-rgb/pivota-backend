-- 135_catalog_product_sku_stale_suppression.sql
--
-- Extend the PR-1 offer suppression primitive to catalog_products and
-- catalog_skus so completed source syncs can tombstone stale rows without
-- destructive deletes. Existing rows are not mutated by this migration.
--
-- NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction block.

ALTER TABLE IF EXISTS catalog_products
  ADD COLUMN IF NOT EXISTS suppression_reason TEXT NULL,
  ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS suppression_metadata JSONB NULL;

ALTER TABLE IF EXISTS catalog_skus
  ADD COLUMN IF NOT EXISTS suppression_reason TEXT NULL,
  ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS suppression_metadata JSONB NULL;

ALTER TABLE IF EXISTS catalog_offers
  ADD COLUMN IF NOT EXISTS suppression_metadata JSONB NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_suppressed
  ON catalog_products (suppressed_at)
  WHERE suppressed_at IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_skus_suppressed
  ON catalog_skus (suppressed_at)
  WHERE suppressed_at IS NOT NULL;

-- DOWN migration is checked in at:
-- db/migrations/down/135_catalog_product_sku_stale_suppression_down.sql
