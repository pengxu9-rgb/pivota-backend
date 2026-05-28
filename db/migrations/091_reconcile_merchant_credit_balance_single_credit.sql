-- Reconcile merchant_credit_balance to the single abstract-credit model.
--
-- Migration 089 briefly created three wallet columns
-- (audit_credits/prompt_credits/execution_credits). The committed pricing
-- model has one customer-facing abstract credit balance; audit/prompt/execution
-- are ledger categories, not separate balances.
--
-- Idempotent. Destructive only for the legacy wallet-column drops, guarded
-- below so a populated three-wallet table cannot be collapsed accidentally.

ALTER TABLE merchant_credit_balance
    ADD COLUMN IF NOT EXISTS credits BIGINT NOT NULL DEFAULT 0 CHECK (credits >= 0);

ALTER TABLE merchant_credit_balance
    ADD COLUMN IF NOT EXISTS usd_cogs_internal NUMERIC(14,4) NOT NULL DEFAULT 0;

ALTER TABLE merchant_credit_balance
    ADD COLUMN IF NOT EXISTS allowance_credits BIGINT NOT NULL DEFAULT 0;

ALTER TABLE merchant_credit_balance
    ADD COLUMN IF NOT EXISTS allowance_period_start TIMESTAMPTZ;

DO $$
BEGIN
    -- Guard the destructive wallet-column drops. The explicit balance check
    -- only runs when all three legacy columns still exist, making this file
    -- re-runnable after the first successful application.
    IF (
        SELECT COUNT(*)
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'merchant_credit_balance'
           AND column_name IN (
               'audit_credits',
               'prompt_credits',
               'execution_credits'
           )
    ) = 3 THEN
        IF EXISTS (
            SELECT 1
              FROM merchant_credit_balance
             WHERE COALESCE(audit_credits, 0)
                 + COALESCE(prompt_credits, 0)
                 + COALESCE(execution_credits, 0) > 0
        ) THEN
            RAISE EXCEPTION
                'Refusing to drop populated merchant_credit_balance wallet columns; run requires feedback_db_access_destructive_ops review';
        END IF;
    END IF;
END $$;

ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS audit_credits;
ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS prompt_credits;
ALTER TABLE merchant_credit_balance DROP COLUMN IF EXISTS execution_credits;

-- Fix plan_tier allow-list: migration 089 omitted 'scale', but the committed
-- Markato model is Starter / Growth / Scale / Enterprise ($99/$299/$999/custom).
-- A Scale-tier merchant could not be inserted under the 089 CHECK. Drop any
-- existing plan_tier CHECK (by definition, name-robust) and re-add the correct
-- superset. Idempotent; superset means existing rows still pass.
DO $$
DECLARE c text;
BEGIN
    FOR c IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'merchant_credit_balance'::regclass
           AND contype = 'c'
           AND pg_get_constraintdef(oid) ILIKE '%plan_tier%'
    LOOP
        EXECUTE format('ALTER TABLE merchant_credit_balance DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE merchant_credit_balance
    ADD CONSTRAINT merchant_credit_balance_plan_tier_check
    CHECK (plan_tier IN ('free','starter','growth','scale','enterprise','custom'));
