-- Migration 185: reconcile x402_transactions with the AP2 transaction routes
-- Date: 2026-07-17
-- Purpose: 023 created x402_transactions with a schema that DIVERGES from what
-- routes/ap2_routes.py actually reads/writes, so the AP2 transaction lifecycle
-- (initiate / confirm + the GET transaction & receipt reads) would 500 against a
-- fresh 023-provisioned database. Same class of drift 182 fixed for
-- nonce_tracker. It went unnoticed because the AP2 surface is flag-gated off and
-- every AP2 test uses an in-memory FakeDB — no test exercises the real schema.
--
-- The routes are the shipped behavior; align the schema to them:
--   * initiate INSERTs `product_id` and `status='pending'`, and does NOT supply
--     `authorization_code`;
--   * confirm sets `status='completed'` and `confirmed_at = NOW()`;
--   * GET transaction/{id} and GET receipt/{id} SELECT `confirmed_at`.
-- 023 lacks `product_id` and `confirmed_at`, has `authorization_code NOT NULL`,
-- and a status CHECK that permits neither 'pending' nor 'completed'.
--
-- Additive + idempotent. schema-guard-exempt: an opt-in AP2 table, applied on
-- demand with the rest of the AP2 schema before ENABLE_AP2_ROUTES=true.

ALTER TABLE IF EXISTS x402_transactions
  ADD COLUMN IF NOT EXISTS product_id VARCHAR(128);

ALTER TABLE IF EXISTS x402_transactions
  ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP WITH TIME ZONE;

-- initiate does not provide authorization_code (assigned later by settlement, if
-- at all), so the original NOT NULL makes every insert fail.
ALTER TABLE IF EXISTS x402_transactions
  ALTER COLUMN authorization_code DROP NOT NULL;

-- Replace the status CHECK so the route lifecycle values are permitted. 023's
-- CHECK is inline/unnamed (Postgres auto-names it), so drop whatever CHECK
-- references `status` by discovered name, then add a permissive one covering both
-- the original x402 vocabulary and the routes' pending/completed/failed. The
-- drop-then-add makes re-runs idempotent (ADD CONSTRAINT has no IF NOT EXISTS).
DO $$
DECLARE constraint_name text;
BEGIN
    IF to_regclass('public.x402_transactions') IS NULL THEN
        RETURN;
    END IF;
    SELECT c.conname INTO constraint_name
    FROM pg_constraint c
    WHERE c.conrelid = 'public.x402_transactions'::regclass
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) ILIKE '%status%';
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE x402_transactions DROP CONSTRAINT %I', constraint_name);
    END IF;
    ALTER TABLE x402_transactions
      ADD CONSTRAINT x402_transactions_status_check
      CHECK (status IN (
        'pending', 'completed', 'failed',
        'authorized', 'captured', 'settled', 'refunded', 'voided'
      ));
END $$;

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 185 completed - x402_transactions reconciled with AP2 routes';
END $$;
