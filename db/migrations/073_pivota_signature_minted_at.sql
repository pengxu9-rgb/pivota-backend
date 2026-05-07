-- Phase C-4 / PR-D: Pivota canonical PDP indexing-arc state.
--
-- Adds `pivota_signature_minted_at TIMESTAMPTZ` to catalog_products
-- so audit reports can compute the real arc phase (fresh / indexing
-- / expected_steady) instead of emitting a static "30-90 day" caveat
-- in merchant_view.diagnosis.indexing_arc_state.
--
-- See services/pivota_indexing_arc.py for the phase boundaries.
--
-- Backfill: existing rows that already have pivota_signature_id (from
-- PR #327's go-forward sync + PR #331's one-shot backfill) get their
-- minted_at set to created_at — best honest guess for legacy rows.
-- Newer rows minted by catalog_sync_service.make_pivota_canonical_fields
-- or by routes/merchant_audit_routes.py's lazy-mint will write a real
-- timestamp at write time.

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS pivota_signature_minted_at TIMESTAMPTZ NULL;

UPDATE catalog_products
   SET pivota_signature_minted_at = created_at
 WHERE pivota_signature_id IS NOT NULL
   AND pivota_signature_minted_at IS NULL;
