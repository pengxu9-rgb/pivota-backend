-- Migration 150: beauty_product_profiles evidence/provenance + disclaimers
-- (K-beauty common-core hardening, claim/provenance + disclaimer schema).
--
-- The agent-decision-grade data contract's Evidence/provenance layer requires
-- each product to carry provenance-backed claims and any required disclaimers,
-- so an agent can justify a recommendation with cited, claim-safe evidence.
--
-- Today beauty_product_profiles.claims_json is a flat array of claim STRINGS
-- with no provenance. This adds two structured JSONB columns alongside it:
--   * evidence_profile  -- { claims: [ProductClaim], review_state } where each
--     claim carries source_ref / source_type / evidence_grade /
--     substantiation_status (see services.claim_safety / models.catalog).
--   * required_disclaimers -- [RequiredDisclaimer] (e.g. the mandatory FDA/DSHEA
--     supplement disclaimer), derived per category_kind.
--
-- Columns are written by the authoring pipeline (the product_intel.v1 envelope
-- writer lives in PIVOTA-Agent -- a separate follow-up). This migration only
-- establishes the backend schema; existing rows stay NULL until authored.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.
BEGIN;

ALTER TABLE beauty_product_profiles ADD COLUMN IF NOT EXISTS evidence_profile JSONB;
ALTER TABLE beauty_product_profiles ADD COLUMN IF NOT EXISTS required_disclaimers JSONB;

COMMIT;
