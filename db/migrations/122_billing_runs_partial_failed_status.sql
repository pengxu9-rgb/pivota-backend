-- 122_billing_runs_partial_failed_status.sql
-- Extend billing_runs.status to include 'partial_failed' so a billing run
-- whose per-merchant invoices partly fail can be retried later without
-- losing the per-merchant idempotency keys (Stripe caches keys for 24h —
-- once the idempotency_key fires, replays return the cached response).
--
-- Without this status, run_billing_cycle marked any run that finished its
-- loop as 'completed' even when some merchants threw exceptions inside,
-- and the run's idempotency_key then prevented a clean retry. Surfaced by
-- codex review of PR #581 (finding #7, high).
--
-- The retry semantics:
--   running        → currently executing
--   partial_failed → loop finished, some merchants didn't get an invoice;
--                    retry resumes only the missing merchants
--   completed      → every eligible merchant has an invoices row
--   failed         → catastrophic failure (e.g. could not even SELECT
--                    merchants); existing semantics unchanged
--   cancelled      → ops cancelled; existing semantics unchanged

ALTER TABLE IF EXISTS billing_runs
  DROP CONSTRAINT IF EXISTS ck_billing_runs_status;

ALTER TABLE IF EXISTS billing_runs
  ADD CONSTRAINT ck_billing_runs_status CHECK (
    status IN ('running', 'completed', 'failed', 'cancelled', 'partial_failed')
  );

-- DOWN (manual rollback): any 'partial_failed' rows must be transitioned
-- to one of the original statuses before re-adding the narrower constraint.
-- ALTER TABLE billing_runs DROP CONSTRAINT IF EXISTS ck_billing_runs_status;
-- ALTER TABLE billing_runs ADD CONSTRAINT ck_billing_runs_status CHECK (
--   status IN ('running', 'completed', 'failed', 'cancelled')
-- );
