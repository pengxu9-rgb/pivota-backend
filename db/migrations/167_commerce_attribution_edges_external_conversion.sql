-- 167_commerce_attribution_edges_external_conversion.sql
-- Tier-2 (T2-2): close the attribution loop for a purchase completed on an
-- un-integrated merchant's OWN Shopify checkout — there is NO Pivota `orders`
-- row for that purchase, so the edge must fully represent a "converted"
-- external order on its own (gap #4 in tier2_v1_build_plan_2026-07-04.md).
--
-- Additive columns only. The internal/PSP path (upsert_order_attribution_edge)
-- leaves every new column NULL/default → its behavior stays byte-identical.
--
-- Idempotent DDL. Prod skips the migration runner — apply via railway ssh /
-- admin (matches migrations 159/166). `ADD COLUMN IF NOT EXISTS` +
-- `CREATE ... IF NOT EXISTS` make re-application a no-op.
--
-- Note: `gross_attributed_gmv_cents` (BIGINT) already exists from migration
-- 109; the external path reuses it. This migration only adds the columns
-- 109 did not: the conversion state machine + the external join/idempotency
-- keys.
BEGIN;

ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS state TEXT,                       -- 'referred' | 'converted' (NULL == legacy internal edge)
  ADD COLUMN IF NOT EXISTS converted_at TIMESTAMPTZ,         -- when the external order reached 'converted'
  ADD COLUMN IF NOT EXISTS currency TEXT,                    -- ISO currency of the external order total
  ADD COLUMN IF NOT EXISTS external_order_id TEXT,           -- the merchant's Shopify order id (no Pivota order exists)
  ADD COLUMN IF NOT EXISTS source TEXT;                      -- 'external_redirect' for T2 conversions

-- `click_id` and `gross_attributed_gmv_cents` already exist (base table +
-- migration 109); declared here defensively so the file is self-describing
-- against a pre-109 schema.
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS click_id TEXT,
  ADD COLUMN IF NOT EXISTS gross_attributed_gmv_cents BIGINT;

-- STRICT idempotency guard: a webhook replay / at-least-once redelivery for the
-- same Shopify order must NOT create a second edge or double-count GMV.
-- Internal edges keep external_order_id = NULL; multi-column NULLs are distinct
-- in Postgres, so this unique index never constrains the internal path.
CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_attribution_edges_external_order
  ON commerce_attribution_edges (merchant_id, external_order_id);

-- Conversion-state lookups (aggregation / seller-trust reads) and click joins.
CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_state_converted
  ON commerce_attribution_edges (state, converted_at);
CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_click_id
  ON commerce_attribution_edges (click_id);

-- Honesty guard: only the two states we write; NULL allowed for legacy rows.
ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_state;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_state
  CHECK (state IS NULL OR state IN ('referred', 'converted'));

COMMENT ON COLUMN commerce_attribution_edges.state IS 'Attribution edge lifecycle: referred | converted (NULL = legacy internal edge)';
COMMENT ON COLUMN commerce_attribution_edges.converted_at IS 'Timestamp an external order reached converted (from orders/paid webhook)';
COMMENT ON COLUMN commerce_attribution_edges.currency IS 'ISO currency of the external order total';
COMMENT ON COLUMN commerce_attribution_edges.external_order_id IS 'Merchant Shopify order id for external conversions (no Pivota orders row)';
COMMENT ON COLUMN commerce_attribution_edges.source IS 'Origin of the edge, e.g. external_redirect for Tier-2 conversions';

COMMIT;

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_state;
-- DROP INDEX IF EXISTS idx_commerce_attribution_edges_click_id;
-- DROP INDEX IF EXISTS idx_commerce_attribution_edges_state_converted;
-- DROP INDEX IF EXISTS uq_commerce_attribution_edges_external_order;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS source;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS external_order_id;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS currency;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS converted_at;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS state;
