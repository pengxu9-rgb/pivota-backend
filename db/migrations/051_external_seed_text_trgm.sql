-- 051_external_seed_text_trgm.sql
--
-- Purpose:
-- 1) Enable trigram acceleration for external seed text matching.
-- 2) Reduce p95 latency for title/domain/url ILIKE scans on active unattached seeds.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_active_title_trgm
  ON external_product_seeds USING GIN (LOWER(title) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_active_domain_trgm
  ON external_product_seeds USING GIN (LOWER(domain) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_active_canonical_url_trgm
  ON external_product_seeds USING GIN (LOWER(canonical_url) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;

CREATE INDEX IF NOT EXISTS idx_external_product_seeds_active_destination_url_trgm
  ON external_product_seeds USING GIN (LOWER(destination_url) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;
