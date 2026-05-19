-- 096_agent_pdp_view_fashion_fields.sql
--
-- Phase O-5b cross-PDP coalesce: extend agent_pdp_view with material /
-- care / size_guide columns (plus their *_source and *_confidence
-- provenance pairs).
--
-- These mirror the catalog_products fashion-field columns added in
-- migration 094 but live on the denormalized canonical view. The
-- services/agent_pdp_view_assembler.py refresh path aggregates values
-- across all product_group_members plus matched external_product_seeds,
-- picking the highest-priority non-NULL source (merchant_payload >
-- merchant_authored > llm_extraction_v1 > external_seed) per field.
--
-- Why coalesce at the canonical-view layer rather than per-row in
-- catalog_products: cross-merchant inheritance must not write inferred
-- values back into the source rows. Merchant A's `material` column
-- represents their own intent (or absence of intent); shadowing it with
-- a value from co-merchant B would race their next platform sync and
-- create silent ownership ambiguity. The canonical view is the right
-- aggregation point — it's read-only from the merchant's perspective.
--
-- Idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Production
-- fast-mode startup skips db/migrations/ so db/schema_guard.py carries
-- the same DDL as the runtime safety net.

ALTER TABLE IF EXISTS agent_pdp_view
  ADD COLUMN IF NOT EXISTS material TEXT,
  ADD COLUMN IF NOT EXISTS material_source VARCHAR(32),
  ADD COLUMN IF NOT EXISTS material_confidence REAL,
  ADD COLUMN IF NOT EXISTS care TEXT,
  ADD COLUMN IF NOT EXISTS care_source VARCHAR(32),
  ADD COLUMN IF NOT EXISTS care_confidence REAL,
  ADD COLUMN IF NOT EXISTS size_guide JSONB,
  ADD COLUMN IF NOT EXISTS size_guide_source VARCHAR(32),
  ADD COLUMN IF NOT EXISTS size_guide_confidence REAL;
