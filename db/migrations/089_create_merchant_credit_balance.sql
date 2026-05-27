-- Spec §I: dedicated merchant audit / prompt / execution credit balances.
--
-- Idempotent and non-destructive. This migration intentionally creates only
-- the new balance table and its index; it does not alter existing usage-event
-- or audit-run tables.

CREATE TABLE IF NOT EXISTS merchant_credit_balance (
    merchant_id        TEXT PRIMARY KEY REFERENCES merchant_onboarding(merchant_id) ON DELETE CASCADE,
    audit_credits      INTEGER NOT NULL DEFAULT 0 CHECK (audit_credits >= 0),
    prompt_credits     INTEGER NOT NULL DEFAULT 0 CHECK (prompt_credits >= 0),
    execution_credits  INTEGER NOT NULL DEFAULT 0 CHECK (execution_credits >= 0),
    plan_tier          TEXT NOT NULL DEFAULT 'free' CHECK (plan_tier IN ('free','starter','growth','enterprise','custom')),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mcb_plan_tier
  ON merchant_credit_balance(plan_tier);
