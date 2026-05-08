-- 075_catalog_products_tags.sql
--
-- Phase O-1 of the PDP onboarding standardization track. Today's gap:
-- StandardProduct.tags[] is populated from merchant platform feeds
-- (Shopify / Wix / WooCommerce) but the ingest pipeline silently drops
-- it. Merchants who diligently tag their products see zero benefit on
-- the canonical surface.
--
-- This migration adds a `tags` column to catalog_products. JSONB to
-- match the existing array-shaped fields on this catalog (compare:
-- catalog_skus.visible_option_labels, catalog_skus.ingredient_ids).
-- Nullable / no default so old rows stay NULL until they're re-synced;
-- the ingest path writes [] (not NULL) for products with no tags so
-- "ingest knew the field was empty" is distinguishable from "row
-- predates the column".
--
-- Future work (separate migrations under the same playbook):
--   - 4 typed columns for the tag taxonomy v1 (price_tier,
--     use_case_tags, lifestyle_tags, demographic) — Phase O-2
--   - LabelAgent to populate these for rows that lack merchant tags —
--     Phase O-3
--   - Lifecycle stage column to gate which rows surface in recall —
--     Phase O-4
--
-- See docs/PDP_ONBOARDING_PLAYBOOK.md.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS tags JSONB;

COMMENT ON COLUMN catalog_products.tags IS
  'Free-form merchant-provided tags from StandardProduct.tags[]. JSONB array. NULL means the row predates the column or was never re-synced; [] means the ingest path saw the merchant feed but found no tags. Future Phase O-3 LabelAgent may extend this for rows where merchant did not provide any.';
