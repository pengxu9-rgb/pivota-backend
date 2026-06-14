-- 152_agent_pdp_view_evidence.sql
-- Project the agent-decision-grade EVIDENCE onto the canonical serving view, so
-- an AI agent reading get_pdp_v2 sees provenance-backed, claim-safe claims +
-- required disclaimers — not just a description. This is the content_key serving
-- path for the supplier-evidence intake (docs/SUPPLIER_EVIDENCE_INTAKE.md):
-- merchant-agnostic, keyed by the canonical content_key, no per-merchant overlay.
--
--   evidence_profile      { claims: [ProductClaim {claim_text, source_ref,
--                          source_type, evidence_grade, substantiation_status}],
--                          review_state }. The intake + beauty_evidence pipeline
--                          write it to beauty_product_profiles.evidence_profile
--                          (per product_key); the agent_pdp_view assembler
--                          selects the HIGHEST-PRECEDENCE one (brand-official /
--                          external_seed > supplier) for the content_key cluster.
--   required_disclaimers  [RequiredDisclaimer] (e.g. the mandatory FDA/DSHEA
--                          supplement disclaimer), derived per category_kind.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Existing rows stay NULL until the
-- assembler re-runs for the content_key.
BEGIN;

ALTER TABLE agent_pdp_view ADD COLUMN IF NOT EXISTS evidence_profile JSONB;
ALTER TABLE agent_pdp_view ADD COLUMN IF NOT EXISTS required_disclaimers JSONB;

COMMENT ON COLUMN agent_pdp_view.evidence_profile IS
  'Provenance-backed claims for the canonical product — highest-precedence evidence_profile across the content_key cluster (brand-official > supplier). Lets an agent justify a recommendation with cited, claim-safe evidence.';
COMMENT ON COLUMN agent_pdp_view.required_disclaimers IS
  'Mandatory disclaimers (e.g. FDA/DSHEA supplement) derived per category_kind; must be surfaced alongside claims.';

COMMIT;
