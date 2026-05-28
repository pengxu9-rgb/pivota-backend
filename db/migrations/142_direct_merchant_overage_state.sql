-- Placeholder migration 142: direct/self-serve overage accrual + hard-stop state.
--
-- This is intentionally scoped to merchant_credit_balance. Do not apply until
-- Claude verifies this placeholder number against concurrent migration PRs.

ALTER TABLE merchant_credit_balance
  ADD COLUMN IF NOT EXISTS overage_pending_credits BIGINT NOT NULL DEFAULT 0;

ALTER TABLE merchant_credit_balance
  ADD COLUMN IF NOT EXISTS overage_charged_credits BIGINT NOT NULL DEFAULT 0;

ALTER TABLE merchant_credit_balance
  ADD COLUMN IF NOT EXISTS overage_blocked_until_payment BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE merchant_credit_balance
  ADD COLUMN IF NOT EXISTS overage_last_payment_intent_id TEXT;

ALTER TABLE merchant_credit_balance
  ADD COLUMN IF NOT EXISTS overage_last_failed_at TIMESTAMPTZ;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conrelid = 'merchant_credit_balance'::regclass
       AND conname = 'ck_merchant_credit_balance_overage_pending_nonnegative'
  ) THEN
    ALTER TABLE merchant_credit_balance
      ADD CONSTRAINT ck_merchant_credit_balance_overage_pending_nonnegative
      CHECK (overage_pending_credits >= 0);
  END IF;

  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conrelid = 'merchant_credit_balance'::regclass
       AND conname = 'ck_merchant_credit_balance_overage_charged_nonnegative'
  ) THEN
    ALTER TABLE merchant_credit_balance
      ADD CONSTRAINT ck_merchant_credit_balance_overage_charged_nonnegative
      CHECK (overage_charged_credits >= 0);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_mcb_direct_overage_sweep
  ON merchant_credit_balance (updated_at)
  WHERE plan_tier <> 'free'
    AND overage_pending_credits > 0
    AND overage_blocked_until_payment = FALSE;

COMMENT ON COLUMN merchant_credit_balance.overage_pending_credits IS
  'Direct/self-serve consumed overage credits not yet successfully charged';

COMMENT ON COLUMN merchant_credit_balance.overage_charged_credits IS
  'Cumulative direct/self-serve overage credits successfully charged';

COMMENT ON COLUMN merchant_credit_balance.overage_blocked_until_payment IS
  'Hard-stop flag set after failed direct overage charge; cleared by successful top-up or overage retry';

-- DOWN (manual rollback only):
-- DROP INDEX IF EXISTS idx_mcb_direct_overage_sweep;
-- ALTER TABLE merchant_credit_balance
--   DROP CONSTRAINT IF EXISTS ck_merchant_credit_balance_overage_pending_nonnegative;
-- ALTER TABLE merchant_credit_balance
--   DROP CONSTRAINT IF EXISTS ck_merchant_credit_balance_overage_charged_nonnegative;
-- ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS overage_last_failed_at;
-- ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS overage_last_payment_intent_id;
-- ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS overage_blocked_until_payment;
-- ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS overage_charged_credits;
-- ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS overage_pending_credits;
