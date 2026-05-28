-- Track the paid top-up portion of the direct/self-serve merchant credit wallet.
--
-- `merchant_credit_balance.credits` remains the spendable aggregate balance.
-- `purchased_credits` is the subset funded by prepaid top-up purchases/refunds
-- and is intentionally preserved when the monthly subscription allowance rolls.
-- Calendar-month allowance is use-it-or-lose-it; top-ups are paid for and persist.

ALTER TABLE merchant_credit_balance
  ADD COLUMN IF NOT EXISTS purchased_credits BIGINT NOT NULL DEFAULT 0;

UPDATE merchant_credit_balance
   SET purchased_credits = 0
 WHERE purchased_credits IS NULL;

ALTER TABLE merchant_credit_balance
  ALTER COLUMN purchased_credits SET DEFAULT 0;

ALTER TABLE merchant_credit_balance
  ALTER COLUMN purchased_credits SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
      FROM pg_constraint
     WHERE conrelid = 'merchant_credit_balance'::regclass
       AND conname = 'ck_merchant_credit_balance_purchased_credits_nonnegative'
  ) THEN
    ALTER TABLE merchant_credit_balance
      ADD CONSTRAINT ck_merchant_credit_balance_purchased_credits_nonnegative
      CHECK (purchased_credits >= 0);
  END IF;
END $$;

COMMENT ON COLUMN merchant_credit_balance.purchased_credits IS
  'Direct/self-serve wallet top-up balance that survives calendar-month allowance reset';

-- DOWN (manual rollback only):
-- ALTER TABLE merchant_credit_balance
--   DROP CONSTRAINT IF EXISTS ck_merchant_credit_balance_purchased_credits_nonnegative;
-- ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS purchased_credits;
