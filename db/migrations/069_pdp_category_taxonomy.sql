-- 069_pdp_category_taxonomy.sql
--
-- Purpose:
-- Phase 2 of the PDP-as-canonical recall migration (see plan at
-- ~/.claude/plans/shimmying-soaring-ember.md and recall investigation
-- handoff at pivota-agent-ui main:reports/recall_v1/RECALL_INVESTIGATION_FINAL.md).
--
-- Adds three columns to catalog_products to support hierarchical category
-- queries — currently the existing `category` column is a single token
-- (e.g. 'Lipstick'), which doesn't compose taxonomy paths. The new
-- category_path stores the full path ('beauty/makeup/lip/lipstick'),
-- enabling prefix queries like LIKE 'beauty/makeup/lip/%' to surface all
-- lip products without enumerating leaf categories.
--
-- category_confidence + category_label_source carry provenance so future
-- enrichment runs can grade & re-process low-confidence rows without
-- clobbering merchant-supplied data.
--
-- Backfill is a separate same-PR Python script
-- (scripts/backfill_pdp_category_path.py) using regex patterns ported from
-- PIVOTA-Agent-mainline-verify/src/services/externalSeedProducts.js.
-- The migration itself just adds the schema; the script does the data work.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS category_path VARCHAR(255),
  ADD COLUMN IF NOT EXISTS category_confidence REAL,
  ADD COLUMN IF NOT EXISTS category_label_source VARCHAR(32);

-- Prefix-friendly index for taxonomy queries (LIKE 'beauty/makeup/lip/%').
CREATE INDEX IF NOT EXISTS idx_catalog_products_category_path_active
  ON catalog_products (category_path varchar_pattern_ops)
  WHERE catalog_track = 'internal_merchant' AND truth_tier = 'primary';

-- Comment helps future reviewers understand provenance values.
COMMENT ON COLUMN catalog_products.category_label_source IS
  'Origin of the labeling: merchant_payload | regex_backfill | enrichment_agent_v1 | manual_review';
COMMENT ON COLUMN catalog_products.category_confidence IS
  '0.0–1.0 confidence in category_path; merchant_payload=1.0, regex_backfill=0.85, enrichment_agent_v1=0.7';
