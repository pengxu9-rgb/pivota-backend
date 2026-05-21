-- 112_partner_balance.sql
-- Cached running partner balance.

CREATE TABLE IF NOT EXISTS partner_balance (
  id BIGSERIAL PRIMARY KEY,
  channel_partner_id BIGINT NOT NULL REFERENCES channel_partners(id) ON DELETE CASCADE,
  balance_cents BIGINT NOT NULL DEFAULT 0,
  last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_partner_balance_channel_partner_id UNIQUE (channel_partner_id)
);

CREATE INDEX IF NOT EXISTS idx_partner_balance_channel_partner_id
  ON partner_balance(channel_partner_id);
CREATE INDEX IF NOT EXISTS idx_partner_balance_last_updated
  ON partner_balance(last_updated DESC);

CREATE OR REPLACE FUNCTION set_partner_balance_last_updated() RETURNS TRIGGER AS $$
BEGIN
  NEW.last_updated = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_partner_balance_last_updated ON partner_balance;
CREATE TRIGGER trg_partner_balance_last_updated
  BEFORE UPDATE ON partner_balance
  FOR EACH ROW EXECUTE PROCEDURE set_partner_balance_last_updated();

COMMENT ON TABLE partner_balance IS 'Monetization v1.3: cached current running balance per channel partner; truth lives in partner_balance_ledger';
COMMENT ON COLUMN partner_balance.balance_cents IS 'Signed partner balance in cents; payouts execute only when balance is positive';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_partner_balance_last_updated ON partner_balance;
-- DROP TABLE IF EXISTS partner_balance;
