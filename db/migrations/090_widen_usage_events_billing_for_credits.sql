-- Spec §I — widen agent_center_usage_events billing CHECK constraints so
-- the merchant credit ledger can write its debit/credit operation rows.
--
-- Brief 3's merchant_credit_balance_service reuses agent_center_usage_events
-- (via its idempotency_key unique index) to dedupe credit operations on
-- replay. It writes billing_mode='debit'|'credit' and billing_status='applied'.
-- The original constraints from migration 067 only allowed:
--   billing_mode   IN ('preview_only', 'metered')
--   billing_status IN ('not_invoiced', 'invoiced', 'voided')
-- so the first credit debit would be rejected at the DB layer.
--
-- The new constraints are a STRICT SUPERSET of the old ones: every existing
-- row already satisfies them, so the ADD CONSTRAINT validation scan finds no
-- violations. On a modest, append-only usage-events table this completes in
-- milliseconds. (If this table ever grows large enough that the validation
-- lock matters, switch to ADD CONSTRAINT ... NOT VALID followed by a separate
-- VALIDATE CONSTRAINT.)
--
-- Idempotent: DROP CONSTRAINT IF EXISTS makes re-running this a no-op.
-- Non-destructive: only the CHECK predicates change; no column or row is
-- dropped or rewritten.
--
-- We intentionally do NOT reuse 'metered'/'not_invoiced' for credit rows
-- (the no-DDL shortcut) because that would silently pollute usage-metering
-- analytics that filter on billing_mode='metered'. Credit-ledger rows get
-- their own explicit vocabulary.

ALTER TABLE agent_center_usage_events
    DROP CONSTRAINT IF EXISTS chk_agent_center_usage_events_billing_mode;
ALTER TABLE agent_center_usage_events
    ADD CONSTRAINT chk_agent_center_usage_events_billing_mode
    CHECK (billing_mode IN ('preview_only', 'metered', 'debit', 'credit'));

ALTER TABLE agent_center_usage_events
    DROP CONSTRAINT IF EXISTS chk_agent_center_usage_events_billing_status;
ALTER TABLE agent_center_usage_events
    ADD CONSTRAINT chk_agent_center_usage_events_billing_status
    CHECK (billing_status IN ('not_invoiced', 'invoiced', 'voided', 'applied'));
