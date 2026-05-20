-- 108_channel_partners.sql
-- Channel partner registry for Markato and future anchor partners.

CREATE TABLE IF NOT EXISTS channel_partners (
  id BIGSERIAL PRIMARY KEY,
  legal_name TEXT NOT NULL,
  contact_email TEXT,
  archetype TEXT NOT NULL,
  stripe_connect_account_id TEXT,
  connect_account_type TEXT,
  commission_config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_channel_partners_legal_name_nonempty CHECK (legal_name <> ''),
  CONSTRAINT ck_channel_partners_archetype CHECK (
    archetype IN ('markato', 'agency', 'affiliate', 'platform', 'protocol_partner', 'other')
  ),
  CONSTRAINT ck_channel_partners_connect_account_type CHECK (
    connect_account_type IS NULL
    OR connect_account_type IN ('standard', 'express', 'custom')
  ),
  CONSTRAINT ck_channel_partners_status CHECK (
    status IN ('pending', 'active', 'inactive', 'suspended')
  )
);

CREATE INDEX IF NOT EXISTS idx_channel_partners_contact_email
  ON channel_partners(contact_email);
CREATE INDEX IF NOT EXISTS idx_channel_partners_archetype
  ON channel_partners(archetype);
CREATE INDEX IF NOT EXISTS idx_channel_partners_status
  ON channel_partners(status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_channel_partners_stripe_connect_account_id
  ON channel_partners(stripe_connect_account_id)
  WHERE stripe_connect_account_id IS NOT NULL;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_channel_partners_updated_at ON channel_partners;
CREATE TRIGGER trg_channel_partners_updated_at
  BEFORE UPDATE ON channel_partners
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE channel_partners IS 'Monetization v1.3: channel partner registry';
COMMENT ON COLUMN channel_partners.commission_config_json IS 'Partner commission rules, subsidy caps, and cohort bonus configuration';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_channel_partners_updated_at ON channel_partners;
-- DROP TABLE IF EXISTS channel_partners;
