-- FIX-04: Switch catalog_skus unique identity from 3-col to 4-col.
-- Per audit: two unvarianted products on same merchant+platform collided on
-- source_variant_id = 'default' under the old index. New 4-col index includes
-- product_key so the collision domain is one-product-wide, not one-merchant-wide.

BEGIN;

-- Build new index alongside the old one (CONCURRENTLY would avoid lock but
-- can't run inside a transaction; this migration uses a brief lock window).
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_skus_source_identity_v2
ON catalog_skus (merchant_id, platform, product_key, source_variant_id);

-- Drop the old 3-col index.
DROP INDEX IF EXISTS idx_catalog_skus_source_identity;

COMMIT;

-- Rollback:
-- 1. Recreate the old index:
--    CREATE UNIQUE INDEX idx_catalog_skus_source_identity
--    ON catalog_skus (merchant_id, platform, source_variant_id);
--    This will fail if post-backfill rows now collide under the old 3-col shape.
-- 2. Revert the app-layer source_variant_id fallback from product_key back to
--    'default'.
-- 3. Drop idx_catalog_skus_source_identity_v2 after the old index is restored.
