-- 157_product_evidence.sql
-- Phase 2a: the cross-vertical merchant evidence store. The generalized twin of
-- beauty_product_profiles.evidence_profile, so merchant-supplied evidence
-- (positioning, lab reports, reviews) flows through the same claim-safe pipeline
-- as ingredient-mechanism claims. UNIONed by the agent_pdp_view assembler onto
-- agent_pdp_view.evidence_profile (the agent-PDP read model). Shared Postgres —
-- PIVOTA-Agent reads these tables directly too. See db/product_evidence.py +
-- docs/ai_readiness_evidence_layer_phase2.md (2a).
--
--   product_evidence.claims  ProductClaim[] (services/claim_safety.py shape:
--                            claim_text, source_ref, source_type, evidence_grade,
--                            substantiation_status). Only `substantiated` claims
--                            are ever served (the serve gate is single-sourced).
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS.
BEGIN;

CREATE TABLE IF NOT EXISTS product_evidence (
  product_key VARCHAR(255) NOT NULL,
  geo_code VARCHAR(16) NOT NULL DEFAULT 'default',
  merchant_id VARCHAR(100),
  claims JSONB,
  review_state VARCHAR(32) NOT NULL DEFAULT 'observed',
  required_disclaimers JSONB,
  created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (product_key, geo_code)
);
CREATE INDEX IF NOT EXISTS idx_product_evidence_merchant ON product_evidence(merchant_id);

COMMENT ON TABLE product_evidence IS
  'Cross-vertical merchant evidence (Phase 2a): provenance-backed ProductClaim[] per (product_key, geo_code), UNIONed with beauty_product_profiles onto agent_pdp_view.evidence_profile.';

CREATE TABLE IF NOT EXISTS evidence_artifact (
  artifact_id VARCHAR(64) PRIMARY KEY,
  product_key VARCHAR(255) NOT NULL,
  merchant_id VARCHAR(100),
  kind VARCHAR(32) NOT NULL,          -- lab_report|certification|review|press|positioning_doc
  source VARCHAR(32) NOT NULL,        -- merchant_upload|web_crawl
  url_or_blob_ref VARCHAR(2000),
  captured_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
  extracted_claim_keys JSONB
);
CREATE INDEX IF NOT EXISTS idx_evidence_artifact_product ON evidence_artifact(product_key);

COMMENT ON TABLE evidence_artifact IS
  'Source documents a product_evidence claim.source_ref points to (lab PDF, cert, review, press, positioning). Written by the 2b merchant intake.';

COMMIT;
