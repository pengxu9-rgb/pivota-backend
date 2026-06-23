-- 161_catalog_products_claim_state.sql
-- P1: claim_state lifecycle on the canonical SKU — the connective tissue between
-- the AUTO audit-seed and the MANUAL claim/attest path.
--   unclaimed     — audit auto-seeded; OBSERVED info only
--   claimed       — a verified brand claim (brand_direct) promoted it
--   attested      — the brand submitted info / lab evidence
--   substantiated — those claims were graded
-- Drives the SYNDICATE-AFTER-CLAIM gate (services/claim_state.py): serve observed
-- info always, but publish/syndicate brand-attested copy only once claimed+.
--
-- Additive + defaulted. Idempotent. Prod skips the migration runner — apply via
-- railway ssh / admin.
BEGIN;

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS claim_state VARCHAR(16) NOT NULL DEFAULT 'unclaimed';

CREATE INDEX IF NOT EXISTS idx_catalog_products_claim_state
  ON catalog_products (merchant_id, claim_state);

COMMIT;
