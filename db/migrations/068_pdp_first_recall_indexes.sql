-- 068_pdp_first_recall_indexes.sql
--
-- Purpose:
-- Recall probe v8 (pivota-agent-ui main:reports/recall_v1/RECALL_INVESTIGATION_FINAL.md)
-- showed shopping_agent pass-rate stuck at 23% with 39/53 Layer-C failures.
-- The architecture target is PDP-first recall (catalog_products as the canonical
-- labeling root), but catalog_products.category currently has no direct B-tree
-- index — only LOWER(brand) / LOWER(title) trigram indexes exist (mig 059).
-- Separately, external_product_seeds.seed_data JSONB has zero indexes; the
-- legacy text-LIKE scan used by _fetch_external_fallback_items eats 12s on a
-- query like "bluetooth earbuds" before returning 0 rows.
--
-- This migration adds 4 indexes to support the PDP-first migration without
-- changing any application code:
--
--   1. B-tree on catalog_products(category) — partial on commerce-ready PDPs.
--      Enables fast PDP-category lookups: "show me lipsticks".
--   2. B-tree on catalog_products(category, brand) — partial composite.
--      Enables fast brand+category compound queries: "MAC lipstick".
--   3. GIN on beauty_product_profiles(taxonomy_json) — currently unindexed
--      JSONB; blocks taxonomy-path queries.
--   4. GIN trigram on LOWER(external_product_seeds.seed_data::text) — closes
--      the legacy CAST-LIKE seq scan path while attached_product_key migration
--      (Phase 3) is in flight.
--
-- Runbook note:
-- - Execute this script in a session with autocommit enabled.
-- - CREATE INDEX CONCURRENTLY cannot run inside a transaction block.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. PDP category lookup
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_category_active
  ON catalog_products (category)
  WHERE catalog_track = 'internal_merchant' AND truth_tier = 'primary';

-- 2. PDP category + brand compound lookup
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_category_brand_active
  ON catalog_products (category, brand)
  WHERE catalog_track = 'internal_merchant' AND truth_tier = 'primary';

-- 3. Beauty profile taxonomy JSONB
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_beauty_product_profiles_taxonomy_json_gin
  ON beauty_product_profiles USING GIN (taxonomy_json jsonb_path_ops);

-- 4. external_product_seeds seed_data text trigram
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_external_product_seeds_active_seed_data_trgm
  ON external_product_seeds USING GIN (LOWER(seed_data::text) gin_trgm_ops)
  WHERE status = 'active' AND attached_product_key IS NULL;
