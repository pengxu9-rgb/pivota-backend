-- 049_products_cache_multi_search_indexes.sql
--
-- Purpose:
-- Speed up cross-merchant find_products_multi recall path.
-- The multi-search path reads a recent bounded slice ordered by cached_at
-- per merchant set, then performs in-memory token matching.
--
-- These indexes keep that recency scan fast without relying on expensive
-- JSON text LIKE scans.

CREATE INDEX IF NOT EXISTS idx_products_cache_merchant_cached_at
  ON products_cache(merchant_id, cached_at DESC);

CREATE INDEX IF NOT EXISTS idx_products_cache_merchant_platform_cached_at
  ON products_cache(merchant_id, platform, cached_at DESC);

