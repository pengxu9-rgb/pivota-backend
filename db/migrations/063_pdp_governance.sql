-- Aggregated SKU PDP governance.
--
-- PDP identity is intentionally independent from merchant products and external
-- seeds. Merchant products and external seeds are sources/offers for a shared
-- projection, not the published PDP itself.

CREATE TABLE IF NOT EXISTS pdp_subject_index (
  pdp_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_ref TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'US',
  product_group_id TEXT NULL,
  external_product_id TEXT NULL,
  representative_product_key TEXT NULL,
  title TEXT NULL,
  image_url TEXT NULL,
  seller_count INTEGER NOT NULL DEFAULT 0,
  external_only BOOLEAN NOT NULL DEFAULT FALSE,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pdp_subject_index_subject
  ON pdp_subject_index(subject_type, subject_ref);

CREATE INDEX IF NOT EXISTS idx_pdp_subject_index_updated
  ON pdp_subject_index(updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_pdp_subject_index_market
  ON pdp_subject_index(market);

CREATE TABLE IF NOT EXISTS pdp_module_versions (
  id TEXT PRIMARY KEY,
  pdp_id TEXT NOT NULL,
  module_key TEXT NOT NULL,
  stage TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'draft',
  payload JSONB NOT NULL,
  source_refs JSONB NULL,
  review_actor_type TEXT NULL,
  review_actor_id TEXT NULL,
  review_model TEXT NULL,
  review_decision TEXT NULL,
  review_confidence DOUBLE PRECISION NULL,
  review_rubric JSONB NULL,
  risk_level TEXT NOT NULL DEFAULT 'low',
  requires_human BOOLEAN NOT NULL DEFAULT FALSE,
  generated_by TEXT NULL,
  generation_ref TEXT NULL,
  created_by_employee_id TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at TIMESTAMPTZ NULL,
  superseded_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_pdp_module_versions_lookup
  ON pdp_module_versions(pdp_id, module_key, stage);

CREATE INDEX IF NOT EXISTS idx_pdp_module_versions_created
  ON pdp_module_versions(created_at DESC);

CREATE TABLE IF NOT EXISTS pdp_audit_log (
  id TEXT PRIMARY KEY,
  pdp_id TEXT NOT NULL,
  module_key TEXT NULL,
  action TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT NULL,
  details JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pdp_audit_log_created
  ON pdp_audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS merchant_pdp_contributions (
  id TEXT PRIMARY KEY,
  pdp_id TEXT NOT NULL,
  product_key TEXT NOT NULL,
  merchant_id TEXT NOT NULL,
  module_key TEXT NOT NULL,
  payload JSONB NOT NULL,
  notes TEXT NULL,
  status TEXT NOT NULL DEFAULT 'submitted',
  reviewed_by_actor_type TEXT NULL,
  reviewed_by_actor_id TEXT NULL,
  review_decision TEXT NULL,
  review_notes TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merchant_pdp_contributions_status
  ON merchant_pdp_contributions(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_merchant_pdp_contributions_product
  ON merchant_pdp_contributions(merchant_id, product_key);
