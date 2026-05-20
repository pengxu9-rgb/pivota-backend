-- 103_extend_merchants_monetization.sql
-- Extend merchants with monetization subscription, tier, and promo state.

ALTER TABLE IF EXISTS merchants
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
  ADD COLUMN IF NOT EXISTS subscription_id BIGINT,
  ADD COLUMN IF NOT EXISTS current_tier TEXT NOT NULL DEFAULT 'free',
  ADD COLUMN IF NOT EXISTS credits_balance BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS current_period_credit_used BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS promo_period_until TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS billing_anchor_day SMALLINT DEFAULT 1;

ALTER TABLE IF EXISTS merchants
  DROP CONSTRAINT IF EXISTS merchants_subscription_id_fkey;
ALTER TABLE IF EXISTS merchants
  ADD CONSTRAINT merchants_subscription_id_fkey
  FOREIGN KEY (subscription_id) REFERENCES user_subscriptions(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS merchants
  DROP CONSTRAINT IF EXISTS ck_merchants_current_tier;
ALTER TABLE IF EXISTS merchants
  ADD CONSTRAINT ck_merchants_current_tier
  CHECK (current_tier IN ('free', 'starter', 'growth', 'scale'));

ALTER TABLE IF EXISTS merchants
  DROP CONSTRAINT IF EXISTS ck_merchants_credits_balance_nonnegative;
ALTER TABLE IF EXISTS merchants
  ADD CONSTRAINT ck_merchants_credits_balance_nonnegative
  CHECK (credits_balance >= 0);

ALTER TABLE IF EXISTS merchants
  DROP CONSTRAINT IF EXISTS ck_merchants_current_period_credit_used_nonnegative;
ALTER TABLE IF EXISTS merchants
  ADD CONSTRAINT ck_merchants_current_period_credit_used_nonnegative
  CHECK (current_period_credit_used >= 0);

ALTER TABLE IF EXISTS merchants
  DROP CONSTRAINT IF EXISTS ck_merchants_billing_anchor_day;
ALTER TABLE IF EXISTS merchants
  ADD CONSTRAINT ck_merchants_billing_anchor_day
  CHECK (billing_anchor_day IS NULL OR billing_anchor_day BETWEEN 1 AND 31);

CREATE INDEX IF NOT EXISTS idx_merchants_stripe_customer_id
  ON merchants(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_merchants_subscription_id
  ON merchants(subscription_id);
CREATE INDEX IF NOT EXISTS idx_merchants_current_tier
  ON merchants(current_tier);
CREATE INDEX IF NOT EXISTS idx_merchants_promo_period_until
  ON merchants(promo_period_until);

COMMENT ON COLUMN merchants.stripe_customer_id IS 'Stripe Customer object ID for monetization billing';
COMMENT ON COLUMN merchants.subscription_id IS 'Current local subscription mirror row';
COMMENT ON COLUMN merchants.current_tier IS 'Current monetization tier: free, starter, growth, or scale';
COMMENT ON COLUMN merchants.credits_balance IS 'Cached current prepaid credit balance';
COMMENT ON COLUMN merchants.current_period_credit_used IS 'Credits consumed during current billing period';
COMMENT ON COLUMN merchants.promo_period_until IS 'Promo take-rate end timestamp; NULL means standard take rate applies';
COMMENT ON COLUMN merchants.billing_anchor_day IS 'Day of month used as the billing cycle anchor';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS merchants DROP CONSTRAINT IF EXISTS merchants_subscription_id_fkey;
-- ALTER TABLE IF EXISTS merchants DROP CONSTRAINT IF EXISTS ck_merchants_current_tier;
-- ALTER TABLE IF EXISTS merchants DROP CONSTRAINT IF EXISTS ck_merchants_credits_balance_nonnegative;
-- ALTER TABLE IF EXISTS merchants DROP CONSTRAINT IF EXISTS ck_merchants_current_period_credit_used_nonnegative;
-- ALTER TABLE IF EXISTS merchants DROP CONSTRAINT IF EXISTS ck_merchants_billing_anchor_day;
-- ALTER TABLE IF EXISTS merchants DROP COLUMN IF EXISTS billing_anchor_day;
-- ALTER TABLE IF EXISTS merchants DROP COLUMN IF EXISTS promo_period_until;
-- ALTER TABLE IF EXISTS merchants DROP COLUMN IF EXISTS current_period_credit_used;
-- ALTER TABLE IF EXISTS merchants DROP COLUMN IF EXISTS credits_balance;
-- ALTER TABLE IF EXISTS merchants DROP COLUMN IF EXISTS current_tier;
-- ALTER TABLE IF EXISTS merchants DROP COLUMN IF EXISTS subscription_id;
-- ALTER TABLE IF EXISTS merchants DROP COLUMN IF EXISTS stripe_customer_id;
