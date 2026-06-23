-- 163_brand_attestation_evidence.sql
-- Durable storage for brand-submitted substantiation evidence (lab reports,
-- certificates). A record starts grading_status='submitted' and is NOT
-- auto-graded; a separate grading step advances it and, when substantiated,
-- advances the SKU's claim_state attested -> substantiated.
-- Idempotent. Prod applies via railway ssh — the migration runner is dev-only.
BEGIN;
CREATE TABLE IF NOT EXISTS brand_attestation_evidence (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(100) NOT NULL,
  product_key VARCHAR(255) NOT NULL,
  content_key VARCHAR(40),
  evidence_ref TEXT NOT NULL,
  evidence_kind VARCHAR(40) NOT NULL DEFAULT 'lab_report',
  grading_status VARCHAR(20) NOT NULL DEFAULT 'submitted',
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  graded_at TIMESTAMPTZ,
  graded_by VARCHAR(64),
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_brand_attest_evidence_product
  ON brand_attestation_evidence (merchant_id, product_key);
CREATE INDEX IF NOT EXISTS idx_brand_attest_evidence_content_key
  ON brand_attestation_evidence (content_key) WHERE content_key IS NOT NULL;
COMMIT;
