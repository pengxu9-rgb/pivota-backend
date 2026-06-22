-- 156_audit_deposit_content_key.sql
-- P0.2 (commerce-index keystone): stamp the canonical entity key (content_key)
-- onto the audit DEPOSIT spine. Today an audit deposits against the
-- storefront-shaped product_key (merchant|platform|source_product_id);
-- content_key — the GTIN-canonical, cross-merchant key that is ALREADY the
-- read key of agent_pdp_view (migration 085) — is computed everywhere but
-- stamped NOWHERE on the deposit, so the audit's exhaust dies as a per-listing
-- blob instead of accreting on the canonical product entity. Stamping it is
-- what makes "audit schema" and "index schema" one object.
--
-- Additive + nullable only: every column is optional, no existing read path
-- changes. Backfill of history is a SEPARATE job
-- (scripts/backfill_audit_content_key.py) with an explicit NULL-key policy —
-- NULL-key rows are COUNTED, never silently dropped, and never back-deposited.
--
-- content_key_basis records HOW the key earned the deposit (gtin /
-- identity_high_conf / reviewed / unresolved) — see
-- services.catalog_identity.resolve_deposit_content_key. Unresolved subjects
-- are gated OUT of entity-scoped deposits to prevent cross-contamination on
-- the DELIBERATELY non-unique content_key (migration 083).
--
-- Idempotent. Prod skips the migration runner — apply via railway ssh / admin.
BEGIN;

-- The run records WHICH canonical entities it audited (parallel to the
-- existing product_keys[] / audited_via_pivota_canonical[]).
ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS content_keys      TEXT[] NULL;
ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS content_key_basis JSONB  NULL;  -- {product_key: basis}

ALTER TABLE evidence_items
  ADD COLUMN IF NOT EXISTS content_key VARCHAR(40) NULL;
CREATE INDEX IF NOT EXISTS idx_evidence_items_content_key
  ON evidence_items (content_key, evidence_type);

ALTER TABLE readiness_findings
  ADD COLUMN IF NOT EXISTS content_key VARCHAR(40) NULL;

ALTER TABLE niche_target_outcomes
  ADD COLUMN IF NOT EXISTS content_key VARCHAR(40) NULL;

-- citation_targets (mig 146) and citation_scan_runs (mig 148) are keyed on
-- merchant_id + sku_key today; add content_key so observations pool against
-- the canonical entity across sellers.
ALTER TABLE citation_targets
  ADD COLUMN IF NOT EXISTS content_key VARCHAR(40) NULL;

ALTER TABLE citation_scan_runs
  ADD COLUMN IF NOT EXISTS content_key VARCHAR(40) NULL;

COMMIT;
