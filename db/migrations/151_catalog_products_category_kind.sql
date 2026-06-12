-- Migration 151: durable catalog_products.category_kind
-- (K-beauty common-core / category-module foundation).
--
-- The data contract requires a durable category_kind in {skincare, haircare,
-- supplement} -- it drives claim-safety screening, required disclaimers, and the
-- serving gate's category checks, so it must be stored, not re-inferred each
-- read. See services.category_kind.resolve_category_kind for the full logic.
--
-- Deterministic backfill here covers skincare/haircare from the beauty
-- category_path. Supplements have no category_path signal, so they're resolved
-- in Python (the sync write path going forward; a backfill applies the resolver
-- to existing rows) -- not guessed in SQL.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + re-runnable backfills.
BEGIN;

ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS category_kind VARCHAR(16);

UPDATE catalog_products
   SET category_kind = 'skincare'
 WHERE lower(category_path) LIKE 'beauty/skincare/%'
   AND category_kind IS NULL;

UPDATE catalog_products
   SET category_kind = 'haircare'
 WHERE lower(category_path) LIKE 'beauty/haircare/%'
   AND category_kind IS NULL;

-- category_kind filters in claim-safety / serving-gate queries.
CREATE INDEX IF NOT EXISTS idx_catalog_products_category_kind
    ON catalog_products (category_kind);

COMMIT;
