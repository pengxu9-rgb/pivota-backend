-- 102_user_subscriptions.sql
-- Merchant subscription mirror for Stripe subscription state.

CREATE TABLE IF NOT EXISTS user_subscriptions (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  plan_id BIGINT NOT NULL REFERENCES subscription_plans(id) ON DELETE RESTRICT,
  stripe_subscription_id TEXT,
  status TEXT NOT NULL DEFAULT 'incomplete',
  started_at TIMESTAMPTZ,
  current_period_start TIMESTAMPTZ,
  current_period_end TIMESTAMPTZ,
  cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
  canceled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_user_subscriptions_stripe_subscription_id UNIQUE (stripe_subscription_id),
  CONSTRAINT ck_user_subscriptions_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_user_subscriptions_status CHECK (
    status IN (
      'incomplete',
      'incomplete_expired',
      'trialing',
      'active',
      'past_due',
      'canceled',
      'unpaid',
      'paused'
    )
  ),
  CONSTRAINT ck_user_subscriptions_period_order CHECK (
    current_period_start IS NULL
    OR current_period_end IS NULL
    OR current_period_end >= current_period_start
  )
);

CREATE INDEX IF NOT EXISTS idx_user_subscriptions_merchant_id
  ON user_subscriptions(merchant_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_plan_id
  ON user_subscriptions(plan_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_status
  ON user_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_current_period_end
  ON user_subscriptions(current_period_end);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_user_subscriptions_updated_at ON user_subscriptions;
CREATE TRIGGER trg_user_subscriptions_updated_at
  BEFORE UPDATE ON user_subscriptions
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE user_subscriptions IS 'Monetization v1.3: per-merchant mirror of Stripe subscription state';
COMMENT ON COLUMN user_subscriptions.merchant_id IS 'Operational merchant identifier used by commerce and payout tables';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_user_subscriptions_updated_at ON user_subscriptions;
-- DROP TABLE IF EXISTS user_subscriptions;
