-- 077_catalog_products_lifecycle_stage.sql
--
-- Phase O-4 of the PDP onboarding standardization track. Adds the
-- column the gateway will eventually filter on (Phase O-5) so only
-- rows that have crossed the validated / published gate surface in
-- recall. Without it, every ingested row is equally surfaced
-- regardless of content depth, taxonomy coverage, or merchant
-- evidence — see docs/PDP_ONBOARDING_PLAYBOOK.md "The eight gaps"
-- (gap #3: pdp_scope='unverified' is the default with no live
-- promotion pipeline).
--
-- Stage vocabulary (matches docs/PDP_ONBOARDING_PLAYBOOK.md):
--
--   draft       — identity present, content insufficient
--                 (no title / no image / description < 50 chars).
--                 Not in recall. Default for newly-ingested rows
--                 with thin content.
--
--   candidate   — title + image + description ≥ 50. Not in recall;
--                 bridge state while taxonomy fills in.
--
--   validated   — candidate + category_path + ≥1 taxonomy signal
--                 (tags / demographic / use_case / lifestyle).
--                 Surfaces in recall at low rank.
--
--   published   — validated + canonical evidence:
--                   pdp_scope='multi_merchant_canonical', OR
--                   source_system='catalog_enrichment_agent_v1', OR
--                   manual approval (future).
--                 Surfaces in recall at full rank.
--
--   hold        — manual / quality-gate flag. Out of recall.
--                 Reserved for future use; v1 doesn't auto-emit.
--
--   archived    — source delisted. Out of recall but row preserved
--                 for sig redirect. Reserved for future use.
--
-- NULL on rows predating this migration. Ingest paths (Phase O-4
-- wires Path A/B/C) compute stage at write time. A future O-7
-- backfill will compute stage for rows still NULL after the
-- onboarding paths have circulated.
--
-- See docs/PDP_ONBOARDING_PLAYBOOK.md.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS pdp_lifecycle_stage VARCHAR(16);

-- Index for the recall side: gateway will filter on
-- `pdp_lifecycle_stage IN ('validated','published')` (Phase O-5).
-- Partial index keeps the index small — most queries care only
-- about live stages, not draft/hold/archived.
CREATE INDEX IF NOT EXISTS idx_catalog_products_lifecycle_live
  ON catalog_products (pdp_lifecycle_stage)
  WHERE pdp_lifecycle_stage IN ('validated', 'published');

COMMENT ON COLUMN catalog_products.pdp_lifecycle_stage IS
  'Onboarding lifecycle stage: draft | candidate | validated | published | hold | archived. NULL on rows predating mig 077. Set at ingest by Path A/B/C; recall filters on validated|published (Phase O-5). See docs/PDP_ONBOARDING_PLAYBOOK.md.';
