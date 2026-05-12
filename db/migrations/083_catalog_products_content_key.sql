-- 083_catalog_products_content_key.sql
--
-- Stage 1 of the PDP architecture roadmap (plans/rosy-mixing-bengio.md).
-- Adds content-derived product identity to catalog_products so the same
-- physical product entering through different paths (Path A Shopify
-- sync, Path B external seed mirror, Path C catalog enrichment, Path D
-- lazy mint during audit) gets the SAME content_key — regardless of
-- merchant_id.
--
-- Why this matters:
--
-- pivota_signature_id (sig_*) is sha256(merchant_id::platform::source_pid).
-- It is deliberately merchant-scoped: Brand X's Shopify store and a
-- scraped external_seed for the same SKU produce DIFFERENT sig_*
-- values. That was the right call when Pivota had a small operator-
-- curated catalog. With many merchants onboarding, it becomes the
-- duplicate-PDP factory: agent UI shows N near-identical cards
-- instead of one card with N offers.
--
-- content_key fixes the identity layer without breaking sig_*. Both
-- columns coexist:
--   - sig_*       — kept for back-compat; URL stays stable
--   - content_key — new; identity across merchants
--
-- Stage 2 will auto-populate product_group_members using content_key
-- matches (services/product_group_autogrouper.py — not in this PR).
-- Stage 3 will key agent_pdp_view on content_key for sub-10ms reads.
--
-- DELIBERATE: this index is NOT UNIQUE. Stage 1 is the *visibility*
-- step — we WANT duplicates to be visible so we can size Stage 2's
-- merge work. Stage 4 may consider tightening to UNIQUE on
-- (content_key, merchant_id) once auto-grouping is stable.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS content_key VARCHAR(40);

-- Partial index: most queries on content_key will be "find duplicates"
-- or "find offers for this product", which only matters for non-null
-- rows. Keeps the index small while content_key population catches up
-- (the backfill won't be instant for ~thousands of rows).
CREATE INDEX IF NOT EXISTS idx_catalog_products_content_key
  ON catalog_products (content_key)
  WHERE content_key IS NOT NULL;

COMMENT ON COLUMN catalog_products.content_key IS
  'Content-derived product identity: ck_<32hex> = sha256(brand_normalized + "::" + title_normalized + "::" + (gtin or "")). Same physical product across merchants/paths produces the same key. Populated at write time by services.catalog_identity.make_content_key (Path A/B/C/D). See plans/rosy-mixing-bengio.md Stage 1. NULL on rows predating migration 083; backfilled by scripts/backfill_content_key.py.';
