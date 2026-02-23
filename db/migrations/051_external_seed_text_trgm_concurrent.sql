-- 051_external_seed_text_trgm_concurrent.sql
--
-- Runbook note:
-- - Execute this script in a session with autocommit enabled.
-- - CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
--
-- Purpose:
-- 1) Enable trigram acceleration for external seed text matching.
-- 2) Provide an ops-safe concurrent index creation path for production.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_external_product_seeds_active_title_trgm
  ON external_product_seeds USING GIN (LOWER(title) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_external_product_seeds_active_domain_trgm
  ON external_product_seeds USING GIN (LOWER(domain) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_external_product_seeds_active_canonical_url_trgm
  ON external_product_seeds USING GIN (LOWER(canonical_url) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_external_product_seeds_active_destination_url_trgm
  ON external_product_seeds USING GIN (LOWER(destination_url) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;
