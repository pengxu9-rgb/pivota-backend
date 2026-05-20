-- 111_partner_attribution.sql
-- Brand-to-channel-partner attribution windows.

CREATE TABLE IF NOT EXISTS partner_attribution (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  channel_partner_id BIGINT NOT NULL REFERENCES channel_partners(id) ON DELETE RESTRICT,
  registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  signed_at TIMESTAMPTZ,
  activated_at TIMESTAMPTZ,
  attribution_window_until TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'registered',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_partner_attribution_merchant_partner UNIQUE (merchant_id, channel_partner_id),
  CONSTRAINT ck_partner_attribution_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_partner_attribution_status CHECK (
    status IN ('registered', 'signed', 'active', 'expired', 'revoked')
  ),
  CONSTRAINT ck_partner_attribution_signed_order CHECK (
    signed_at IS NULL OR signed_at >= registered_at
  ),
  CONSTRAINT ck_partner_attribution_activated_order CHECK (
    activated_at IS NULL OR signed_at IS NULL OR activated_at >= signed_at
  )
);

CREATE INDEX IF NOT EXISTS idx_partner_attribution_merchant_id
  ON partner_attribution(merchant_id);
CREATE INDEX IF NOT EXISTS idx_partner_attribution_channel_partner_id
  ON partner_attribution(channel_partner_id);
CREATE INDEX IF NOT EXISTS idx_partner_attribution_status
  ON partner_attribution(status);
CREATE INDEX IF NOT EXISTS idx_partner_attribution_window_until
  ON partner_attribution(attribution_window_until);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_partner_attribution_updated_at ON partner_attribution;
CREATE TRIGGER trg_partner_attribution_updated_at
  BEFORE UPDATE ON partner_attribution
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE partner_attribution IS 'Monetization v1.3: merchant-to-channel-partner attribution windows';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_partner_attribution_updated_at ON partner_attribution;
-- DROP TABLE IF EXISTS partner_attribution;
