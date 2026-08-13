-- 165_index_pipeline_state_index_eligible.sql
-- ADR-007 SLICE 1: decouple CITATION from COMMERCE.
--
-- `index_pipeline_state.serving_eligible` (mig 098) means "has a buyable offer":
-- it requires has_price (a catalog_offers row). That binds agent VISIBILITY to a
-- live shopping offer, which is wrong for the citation surface — a brand can be
-- worth citing without us carrying a buyable offer for it.
--
-- `index_eligible` is the PARALLEL, OFFER-FREE quality+trust+identity floor that
-- drives the citation READ surface. It is the full serving_eligible predicate set
-- MINUS has_price:
--   content_quality_score >= QUALITY_SCORE_THRESHOLD (see
--   services/index_pipeline_state_service.py; 71.4 since #1612, NOT the 65 this
--   line used to state) AND has_image AND description_length >= 50 AND
--   identity_resolved AND source_document_present AND seed_audit OK AND
--   not domain regression AND catalog_products.sync_status = 'live'.
-- has_image is REQUIRED (a citation without an image is not a usable PDP).
--
-- ADDITIVE + defaulted FALSE. serving_eligible is UNCHANGED (still requires
-- has_price). Idempotent. Prod skips the migration runner — apply via
-- railway ssh / admin (mirrors routes/admin_run_migration_098.py).
BEGIN;

ALTER TABLE index_pipeline_state
  ADD COLUMN IF NOT EXISTS index_eligible BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN index_pipeline_state.index_eligible IS
  'ADR-007: offer-free citation eligibility. serving_eligible MINUS has_price (trust+quality+identity+image). Drives the citation read surface; does NOT gate commerce/shopping/checkout.';

-- Partial index mirroring idx_ips_serving_eligible (mig 098) so the citation
-- read gates (serving_eligible OR index_eligible) stay index-backed.
CREATE INDEX IF NOT EXISTS idx_ips_index_eligible
  ON index_pipeline_state (index_eligible)
  WHERE index_eligible = TRUE;

COMMIT;
