-- 139_catalog_merchants_indexable.sql
--
-- `catalog_merchants.indexable = FALSE` means the merchant must not appear in
-- public discovery surfaces: product sitemap, llms.txt, agent discovery, or
-- other crawler-facing surfaces. This is a merchant-wide public-indexing gate,
-- intended for non-transactable test stores and similar cohorts.
--
-- Public surfacing is a two-layer model:
--
--   1. catalog_merchants.indexable is the merchant-wide gate.
--   2. index_pipeline_state.serving_eligible is the per-content-key gate.
--
-- A product row must pass BOTH gates before it can surface publicly.
--
-- `catalog_merchants.status` remains separate from `indexable`: an active
-- merchant can still be non-indexable (for example, test stores), and an
-- inactive merchant should not surface publicly either. Public read paths must
-- also filter on status='active'.

ALTER TABLE catalog_merchants
  ADD COLUMN IF NOT EXISTS indexable BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN catalog_merchants.indexable IS
  'Merchant-wide public discovery gate. FALSE excludes products from public sitemap, llms.txt, agent discovery, and crawler-facing surfaces even when per-content-key serving_eligible is TRUE.';

CREATE INDEX IF NOT EXISTS idx_catalog_merchants_indexable
  ON catalog_merchants (indexable) WHERE indexable IS FALSE;

-- Existing known test merchant (MOYU/Chydan). Non-transactable Shopify
-- Payments setup since 2026-05-12 -- see pivota-agent-ui/src/app/sitemap-seeds.ts
-- TEST_MERCHANT_IDS history.
UPDATE catalog_merchants
SET indexable = FALSE
WHERE merchant_id = 'merch_efbc46b4619cfbdf';
