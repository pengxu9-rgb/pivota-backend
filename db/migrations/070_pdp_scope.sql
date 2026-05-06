-- 070_pdp_scope.sql
--
-- Phase 6 of the PDP-as-canonical recall migration. After Phases 1–4
-- landed, the deterministic matcher dry-run on 500 unattached external
-- seeds matched zero. Catalog audit revealed 80% of catalog_products is
-- private inventory from one Shopify test merchant (1216 MOYU brushes),
-- which the schema treats identically to multi-merchant canonical PDPs
-- (e.g. "MAC Russian Red Lipstick"). This migration adds a first-class
-- column to distinguish the two so the matcher and recall can rely on
-- it.
--
-- Without this, the matcher could silently cross-attach a Sigma seed
-- onto a MOYU PDP via trigram, and recall ranks every internal_merchant
-- row equally regardless of whether the brand has industry coverage.
--
--   pdp_scope values:
--     'multi_merchant_canonical' — industry product, multiple merchants
--                                  offer it. Recall default. Matcher
--                                  cross-attaches seeds.
--     'merchant_owned'           — single merchant's exclusive listing.
--                                  DTC, white-label, custom. Recall
--                                  surfaces only on merchant-scoped or
--                                  explicit-brand queries. Matcher does
--                                  NOT cross-attach.
--     'unverified'               — newly inserted, classification
--                                  deferred. Treated as merchant_owned
--                                  for matcher safety until backfill.
--
-- Provenance columns track WHO set the scope and WHEN, so a future
-- elevation job can find rows safe to upgrade (e.g. a merchant_owned
-- row whose product later appears at a second merchant).

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS pdp_scope VARCHAR(32) NOT NULL DEFAULT 'unverified',
  ADD COLUMN IF NOT EXISTS pdp_scope_source VARCHAR(32),
  ADD COLUMN IF NOT EXISTS pdp_scope_set_at TIMESTAMPTZ;

-- Hot index for the matcher and generic recall — only canonical rows
-- need to be fast for category+brand lookups, since merchant_owned
-- rows are surfaced via merchant_id-scoped queries (covered below).
CREATE INDEX IF NOT EXISTS idx_catalog_products_canonical_active
  ON catalog_products (category, brand)
  WHERE truth_tier = 'primary'
    AND catalog_track = 'internal_merchant'
    AND pdp_scope = 'multi_merchant_canonical';

-- Reverse index for merchant-scoped queries (e.g. "show me MOYU brushes").
CREATE INDEX IF NOT EXISTS idx_catalog_products_merchant_owned_active
  ON catalog_products (merchant_id, category)
  WHERE truth_tier = 'primary'
    AND pdp_scope = 'merchant_owned';

COMMENT ON COLUMN catalog_products.pdp_scope IS
  'multi_merchant_canonical | merchant_owned | unverified — see mig 070';
COMMENT ON COLUMN catalog_products.pdp_scope_source IS
  'Origin of the scope label: backfill_2026_05 | enrichment_agent_v1 | merchant_sync | manual_review';
COMMENT ON COLUMN catalog_products.pdp_scope_set_at IS
  'Timestamp when pdp_scope was last set; supports elevation/audit jobs';
