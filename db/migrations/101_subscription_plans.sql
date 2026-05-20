-- 101_subscription_plans.sql
-- Plan catalogue for monetization v1.3.

CREATE TABLE IF NOT EXISTS subscription_plans (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  stripe_price_id TEXT,
  price_cents BIGINT NOT NULL DEFAULT 0,
  tier_level SMALLINT NOT NULL,
  monthly_credit_allowance BIGINT NOT NULL DEFAULT 0,
  features_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_subscription_plans_name UNIQUE (name),
  CONSTRAINT uq_subscription_plans_stripe_price_id UNIQUE (stripe_price_id),
  CONSTRAINT ck_subscription_plans_name CHECK (name IN ('free', 'starter', 'growth', 'scale')),
  CONSTRAINT ck_subscription_plans_price_cents CHECK (price_cents >= 0),
  CONSTRAINT ck_subscription_plans_tier_level CHECK (tier_level BETWEEN 0 AND 3),
  CONSTRAINT ck_subscription_plans_monthly_credit_allowance CHECK (monthly_credit_allowance >= 0),
  CONSTRAINT ck_subscription_plans_status CHECK (status IN ('active', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_subscription_plans_status
  ON subscription_plans(status);
CREATE INDEX IF NOT EXISTS idx_subscription_plans_tier_level
  ON subscription_plans(tier_level);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_subscription_plans_updated_at ON subscription_plans;
CREATE TRIGGER trg_subscription_plans_updated_at
  BEFORE UPDATE ON subscription_plans
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE subscription_plans IS 'Monetization v1.3: free/starter/growth/scale subscription plan catalogue';
COMMENT ON COLUMN subscription_plans.features_json IS 'Flexible plan feature flags and limits';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_subscription_plans_updated_at ON subscription_plans;
-- DROP TABLE IF EXISTS subscription_plans;
