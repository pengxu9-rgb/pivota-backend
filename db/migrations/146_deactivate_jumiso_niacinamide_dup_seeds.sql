-- 146_deactivate_jumiso_niacinamide_dup_seeds.sql
--
-- Collapse the Jumiso USA "20% NIACINAMIDE High Potency Dark Spot Serum"
-- external-seed duplicate cluster down to a single canonical row.
--
-- Context
-- -------
-- The brand crawler (external_brand_crawl) seeded jumiso.us faithfully,
-- but the merchant's own Shopify store carries ~21 duplicate listings of
-- this one product — the base handle plus a chain of admin duplicates
-- whose titles/handles leak junk suffixes:
--   (Copy), (Copy_b)..(Copy_h), (Copy_e) [malformed, no close paren],
--   (Copy_T1)..(Copy_T7), (Convert_a), and bare-title re-lists on the
--   -1 / -2 / -3 / new-...-copy handles.
-- All 21 landed as active external_product_seeds under the singleton
-- synthetic merchant 'external_seed' and were mirrored into
-- catalog_products. Each distinct title mints a distinct content_key, so
-- the serving-side near-dup collapse (PIVOTA-Agent #1738/#1739/#1740)
-- demotes the exact-title dupes but leaks the 16 suffix/malformed copies
-- into search + PLP for "niacinamide serum for dark spots".
--
-- These are not real distinct products and not Pivota test/tripwire rows
-- (no test or fixture references them); they are a crawl of a merchant's
-- duplicate-listing mess. Root cause = the fake rows should not serve.
--
-- Canonical
-- ---------
-- Keep the clean base Shopify handle
--   https://jumiso.us/products/20-niacinamide-high-potency-dark-spot-serum
-- = external_product_seeds.id
--   'external_brand_crawl::jumiso_us_8485953503393'
-- (title "20% NIACINAMIDE High Potency Dark Spot Serum", in_stock,
--  content_key ck_560489fee9bed01714e16dcf3fd2310d, shared by the 5
--  exact-title siblings that already collapse in serving).
--
-- Two mirrors — why both statements are required
-- ----------------------------------------------
-- 1. external_product_seeds gates the recall / affiliate-offer path
--    (routes/agent_shop_gateway.py: WHERE status='active') and is the
--    source the mirror reads (scripts/mirror_external_seeds_to_catalog_products.py
--    SELECTs lower(status)='active'). status='inactive' removes the row
--    from recall AND from future mirror passes.
-- 2. catalog_products gates the find_products_multi / PLP+search path.
--    The mirror inserts ON CONFLICT DO NOTHING and NEVER tombstones a
--    seed that dropped out of 'active', and the stale-catalog sweep
--    (scripts/sweep_stale_catalog_products.py) EXPLICITLY EXCLUDES
--    'external_seed'. So the mirror row must be suppressed explicitly —
--    same lever as 139_tombstone_cross_merchant_redundant_external_seed.sql.
--    The catalog_row_trust policy gates serving on suppression_reason.
--
-- Idempotent: guarded by status='active' / suppression_reason IS NULL, so
-- re-runs are no-ops. If the crawler re-inserts new duplicates later (its
-- own dedup is the deeper fix, tracked separately), re-applying this
-- migration re-collapses the group.
--
-- NOTE: production fast-mode startup skips db/migrations/, so this was
-- also applied directly to prod (Postgres-xMr6) via
-- scripts/cleanup_niacinamide_test_variants.py --apply. This file is the
-- reviewable artifact and covers envs that run raw migrations.

BEGIN;

-- 1) Deactivate the 20 duplicate seeds; keep the canonical.
UPDATE external_product_seeds
SET status = 'inactive', updated_at = now()
WHERE status = 'active'
  AND title ILIKE '%High Potency Dark Spot Serum%'
  AND id <> 'external_brand_crawl::jumiso_us_8485953503393';

-- 2) Suppress the 20 mirror rows; keep the canonical's mirror live.
--    catalog_products.source_ref == external_product_seeds.id for this
--    mirror path (see mirror script offer/product source_ref mapping).
UPDATE catalog_products
SET suppression_reason = 'niacinamide_dup_test_variant',
    -- P1a (#1648): see migration 139.
    suppressed_at = COALESCE(suppressed_at, now()),
    updated_at = now()
WHERE merchant_id = 'external_seed'
  AND suppression_reason IS NULL
  AND title ILIKE '%High Potency Dark Spot Serum%'
  AND source_ref <> 'external_brand_crawl::jumiso_us_8485953503393';

COMMIT;
