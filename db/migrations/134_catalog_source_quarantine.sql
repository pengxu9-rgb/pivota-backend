-- 134_catalog_source_quarantine.sql
-- Source quarantine overlay for opt-in reader anti-joins.
-- schema-guard-exempt: creates a new opt-in table; no runtime ADD COLUMN self-heal needed.

CREATE TABLE IF NOT EXISTS catalog_source_quarantine (
  quarantine_id BIGSERIAL PRIMARY KEY,
  match_type TEXT NOT NULL CHECK (match_type IN ('domain','merchant_platform','source_system_ref')),
  match_value TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','revoked','expired')),
  reason TEXT,
  expires_at TIMESTAMPTZ,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  revoked_by TEXT,
  metadata JSONB
);

COMMENT ON TABLE catalog_source_quarantine IS
  'Opt-in source quarantine overlay. Readers may anti-join this table to suppress inactive or unsafe catalog sources without changing source ingestion lifecycle.';

COMMENT ON COLUMN catalog_source_quarantine.match_type IS
  'domain | merchant_platform | source_system_ref. See services/source_quarantine.py for match value conventions.';

CREATE INDEX IF NOT EXISTS idx_csq_active_lookup
  ON catalog_source_quarantine (match_type, lower(match_value))
  WHERE state = 'active';

CREATE INDEX IF NOT EXISTS idx_csq_match_value_lower
  ON catalog_source_quarantine (lower(match_value));
