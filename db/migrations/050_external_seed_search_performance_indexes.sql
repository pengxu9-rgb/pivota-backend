-- 050_external_seed_search_performance_indexes.sql
--
-- Purpose:
-- 1) Speed up active external seed recency scans used by product search fallback paths.
-- 2) Improve cross-merchant recency reads when merchant filter is absent.

-- External seed recency (active, unattached rows first).
CREATE INDEX IF NOT EXISTS idx_external_product_seeds_active_market_updated_at
  ON external_product_seeds(market, updated_at DESC)
  WHERE status = 'active' AND attached_product_key IS NULL;

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_active_updated_at
  ON external_product_seeds(updated_at DESC)
  WHERE status = 'active' AND attached_product_key IS NULL;

-- Cross-merchant recency on products cache (no merchant predicate path).
CREATE INDEX IF NOT EXISTS idx_products_cache_cached_at_desc
  ON products_cache(cached_at DESC);

CREATE INDEX IF NOT EXISTS idx_products_cache_platform_cached_at_desc
  ON products_cache(platform, cached_at DESC);
