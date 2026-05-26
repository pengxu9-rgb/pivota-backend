-- Down migration for 135_catalog_product_sku_stale_suppression.sql.
-- This removes only the schema added for stale sync tombstones. It does
-- not restore or mutate catalog data.
--
-- NOTE: DROP INDEX CONCURRENTLY cannot run inside a transaction block.

DROP INDEX CONCURRENTLY IF EXISTS idx_catalog_products_suppressed;
DROP INDEX CONCURRENTLY IF EXISTS idx_catalog_skus_suppressed;

ALTER TABLE IF EXISTS catalog_offers
  DROP COLUMN IF EXISTS suppression_metadata;

ALTER TABLE IF EXISTS catalog_skus
  DROP COLUMN IF EXISTS suppression_metadata,
  DROP COLUMN IF EXISTS suppressed_at,
  DROP COLUMN IF EXISTS suppression_reason;

ALTER TABLE IF EXISTS catalog_products
  DROP COLUMN IF EXISTS suppression_metadata,
  DROP COLUMN IF EXISTS suppressed_at,
  DROP COLUMN IF EXISTS suppression_reason;
