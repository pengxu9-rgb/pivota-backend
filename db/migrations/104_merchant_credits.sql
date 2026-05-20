-- 104_merchant_credits.sql
-- Cached merchant prepaid credit balance and auto-topup configuration.

CREATE TABLE IF NOT EXISTS merchant_credits (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  balance BIGINT NOT NULL DEFAULT 0,
  tier_allowance BIGINT NOT NULL DEFAULT 0,
  period_start TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  auto_topup_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  auto_topup_threshold BIGINT NOT NULL DEFAULT 0,
  auto_topup_amount_credits BIGINT NOT NULL DEFAULT 0,
  last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_merchant_credits_merchant_id UNIQUE (merchant_id),
  CONSTRAINT ck_merchant_credits_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_merchant_credits_balance_nonnegative CHECK (balance >= 0),
  CONSTRAINT ck_merchant_credits_tier_allowance_nonnegative CHECK (tier_allowance >= 0),
  CONSTRAINT ck_merchant_credits_auto_topup_threshold_nonnegative CHECK (auto_topup_threshold >= 0),
  CONSTRAINT ck_merchant_credits_auto_topup_amount_nonnegative CHECK (auto_topup_amount_credits >= 0),
  CONSTRAINT ck_merchant_credits_auto_topup_config CHECK (
    auto_topup_enabled = FALSE
    OR auto_topup_amount_credits > 0
  )
);

CREATE INDEX IF NOT EXISTS idx_merchant_credits_period_start
  ON merchant_credits(period_start);
CREATE INDEX IF NOT EXISTS idx_merchant_credits_merchant_id
  ON merchant_credits(merchant_id);
CREATE INDEX IF NOT EXISTS idx_merchant_credits_auto_topup
  ON merchant_credits(auto_topup_enabled)
  WHERE auto_topup_enabled = TRUE;

CREATE OR REPLACE FUNCTION set_merchant_credits_last_updated() RETURNS TRIGGER AS $$
BEGIN
  NEW.last_updated = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_merchant_credits_last_updated ON merchant_credits;
CREATE TRIGGER trg_merchant_credits_last_updated
  BEFORE UPDATE ON merchant_credits
  FOR EACH ROW EXECUTE PROCEDURE set_merchant_credits_last_updated();

COMMENT ON TABLE merchant_credits IS 'Monetization v1.3: cached prepaid credit balance and auto-topup settings per merchant';
COMMENT ON COLUMN merchant_credits.balance IS 'Current prepaid credit balance';
COMMENT ON COLUMN merchant_credits.auto_topup_enabled IS 'When true, metering can trigger a topup below threshold';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_merchant_credits_last_updated ON merchant_credits;
-- DROP TABLE IF EXISTS merchant_credits;
