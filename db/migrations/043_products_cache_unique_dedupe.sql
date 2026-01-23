-- 043_products_cache_unique_dedupe.sql
--
-- Problem:
-- Concurrent sync/import jobs can create duplicate rows in products_cache for the same
-- (merchant_id, platform, platform_product_id) because the application previously used
-- a non-atomic read-then-insert pattern without a uniqueness constraint.
--
-- Fix:
-- 1) Remove invalid rows with missing product IDs.
-- 2) Deduplicate existing rows, keeping the latest cached_at (then highest id).
-- 3) Add a unique index to prevent future duplicates and enable ON CONFLICT upserts.

DELETE FROM products_cache
WHERE platform_product_id IS NULL OR platform_product_id = '';

WITH ranked AS (
  SELECT
    id,
    ROW_NUMBER() OVER (
      PARTITION BY merchant_id, platform, platform_product_id
      ORDER BY cached_at DESC, id DESC
    ) AS rn
  FROM products_cache
)
DELETE FROM products_cache
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

CREATE UNIQUE INDEX IF NOT EXISTS uq_products_cache_merchant_platform_product
ON products_cache (merchant_id, platform, platform_product_id);

