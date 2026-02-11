-- PDP/resolve chain index hardening.
-- Focus: reduce lookup latency for /agent/v1/products/search + resolve + offers.resolve.

CREATE INDEX IF NOT EXISTS idx_products_cache_platform_product_id
  ON products_cache(platform_product_id);

CREATE INDEX IF NOT EXISTS idx_products_cache_product_data_id
  ON products_cache((product_data->>'id'));

CREATE INDEX IF NOT EXISTS idx_products_cache_product_data_product_id
  ON products_cache((product_data->>'product_id'));

CREATE INDEX IF NOT EXISTS idx_products_cache_merchant_expires_cached
  ON products_cache(merchant_id, expires_at DESC, cached_at DESC);

CREATE INDEX IF NOT EXISTS idx_product_group_members_platform_product_id
  ON product_group_members(platform_product_id);
