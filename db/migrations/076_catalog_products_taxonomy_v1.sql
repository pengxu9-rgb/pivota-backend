-- 076_catalog_products_taxonomy_v1.sql
--
-- Phase O-2 of the PDP onboarding standardization track. After O-1
-- (mig 075) wired free-form merchant `tags[]` through all three
-- onboarding paths (Shopify ingest / external seed mirror / catalog
-- enrichment agent), recall + filtering still has nothing to facet on.
-- This migration adds 4 typed columns that capture the taxonomy
-- dimensions a real e-commerce search needs:
--
--   price_tier      VARCHAR(16) NULL
--     Deterministic transform of product.price into one of:
--       under_50, 50_100, 100_200, 200_500, 500_plus, unknown
--     Always derivable at ingest time, no LLM needed.
--
--   use_case_tags   JSONB NULL
--     Free-form list of normalized tokens describing intended usage:
--       daily, special_occasion, gift, professional, sport, travel
--     Conservative deterministic extraction in O-2; Phase O-3
--     LabelAgent will fill the long tail from product content.
--
--   lifestyle_tags  JSONB NULL
--     Free-form list of normalized lifestyle / values tokens:
--       vegan, cruelty_free, sustainable, fragrance_free,
--       hypoallergenic, organic, paraben_free, etc.
--     Same conservative-extraction-then-LabelAgent pattern.
--
--   demographic     VARCHAR(16) NULL
--     One of: women, men, unisex, kids
--     NULL when no clear signal — LabelAgent fills later.
--
-- All NULL on rows predating this migration. Phase O-1's free-form
-- `tags` column (mig 075) stays untouched — it carries the
-- merchant's literal labels; these new columns are Pivota's normalized
-- derivation. Both feed into recall + facets.
--
-- See docs/PDP_ONBOARDING_PLAYBOOK.md (Decision 1: Hybrid taxonomy v1).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS price_tier VARCHAR(16),
  ADD COLUMN IF NOT EXISTS use_case_tags JSONB,
  ADD COLUMN IF NOT EXISTS lifestyle_tags JSONB,
  ADD COLUMN IF NOT EXISTS demographic VARCHAR(16);

COMMENT ON COLUMN catalog_products.price_tier IS
  'Deterministic price bucket: under_50 | 50_100 | 100_200 | 200_500 | 500_plus | unknown. Computed at ingest from product price. NULL on rows predating mig 076.';

COMMENT ON COLUMN catalog_products.use_case_tags IS
  'Pivota-normalized usage tokens (JSONB array): daily, special_occasion, gift, professional, sport, travel, etc. v1 uses conservative deterministic extraction; Phase O-3 LabelAgent fills the long tail.';

COMMENT ON COLUMN catalog_products.lifestyle_tags IS
  'Pivota-normalized lifestyle / values tokens (JSONB array): vegan, cruelty_free, sustainable, fragrance_free, hypoallergenic, organic, paraben_free, etc.';

COMMENT ON COLUMN catalog_products.demographic IS
  'One of women | men | unisex | kids. NULL when ingest had no clear signal — Phase O-3 LabelAgent will fill from content.';
