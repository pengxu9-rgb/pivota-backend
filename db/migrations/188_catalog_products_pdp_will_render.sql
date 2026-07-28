-- 188: persist "will agent.pivota.cc/products/{sig} render?" at ROW grain.
--
-- WHY catalog_products AND NOT index_pipeline_state.
-- The original plan was to hang this off index_pipeline_state so consumers could
-- add `AND ips.<col> = TRUE`. That table's PRIMARY KEY is (content_key)
-- (migration 098), and the predicate is NOT content_key-grain:
--
--     pdp_will_render = pdp_renderable_expression   (CONTENT ROUTE, row-grain:
--                                                    resolves per catalog_products
--                                                    row through external_product_seeds
--                                                    on source_product_id / product_key)
--                   AND pdp_serving_gate_passes     (content_key-grain)
--
-- A composite of a row fact and a key fact is a row fact. Measured on prod
-- 2026-07-28: of 11,288 content_keys, 1,289 have more than one catalog_products
-- row, and 279 of those have rows that DISAGREE on pdp_will_render. For those
-- 279 there is no correct single per-content_key value — ANY re-advertises dead
-- sigs (the bug this exists to close), ALL suppresses live ones.
--
-- Confirmed over HTTP rather than only by predicate — both sides of three
-- disagreeing keys, 6/6 matching the row-grain prediction:
--     sig_817ed740… 200 / sig_aa918d2d… 404   (same content_key)
--     sig_5e889384… 200 / sig_1d40a380… 404   (same content_key)
--     sig_70466b35… 200 / sig_a8712550… 404   (same content_key)
--
-- catalog_products is PK (product_key), carries pivota_signature_id (the thing
-- actually advertised), and is already joined by every consumer.
--
-- FAIL CLOSED. Nullable with NO default on purpose: NULL means "never computed",
-- and consumers must gate on `IS TRUE` — never `IS NOT FALSE`, which would treat
-- an uncomputed row as advertisable. A DEFAULT would erase the distinction
-- between "computed false" and "never computed", which is the one distinction
-- that keeps this fail-closed.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS pdp_will_render BOOLEAN;

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS pdp_will_render_computed_at TIMESTAMPTZ;

-- Partial index on the advertisable set only. Consumers ask
-- `WHERE pdp_will_render IS TRUE`, and on prod that is the minority side
-- (5,008 true / 9,096 false of 14,104 rows), so indexing only TRUE keeps it
-- small while covering every read this column exists for.
CREATE INDEX IF NOT EXISTS idx_catalog_products_pdp_will_render_true
  ON catalog_products (product_key)
  WHERE pdp_will_render IS TRUE;

COMMENT ON COLUMN catalog_products.pdp_will_render IS
  'Row-grain: will agent.pivota.cc/products/{pivota_signature_id} return 200? '
  'Written ONLY by services.pdp_renderability_store from '
  'services.pdp_renderability.pdp_will_render_expression — never recomputed '
  'independently, or it becomes a fourth drifting twin. NULL = never computed = '
  'do not advertise. Consumers MUST gate on IS TRUE, never IS NOT FALSE. '
  'Can be stale: see pdp_will_render_computed_at and the freshness contract in '
  'services/pdp_renderability_store.py.';

COMMENT ON COLUMN catalog_products.pdp_will_render_computed_at IS
  'When pdp_will_render was last written. Exists so staleness is MEASURABLE '
  'rather than assumed: the composite depends on index_pipeline_state, which is '
  'reconciled nightly at 04:00 UTC with off-cron trickle, so a stored value can '
  'lag reality. No consumer may trust the boolean without checking this age.';
