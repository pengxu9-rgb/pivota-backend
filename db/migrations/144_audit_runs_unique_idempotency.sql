-- Migration 144: DB-enforced audit idempotency. Closes the check-then-insert
-- race in POST /api/audits where two concurrent POSTs with the same
-- payload both miss find_in_flight_by_idempotency_key and both
-- enqueue full audits → 2× LLM cost for one customer ask.
--
-- The existing idempotency index from 083 is non-unique:
--   idx_merchant_audit_runs_idempotency
--     ON merchant_audit_runs (idempotency_key)
--     WHERE idempotency_key IS NOT NULL
--
-- Add a PARTIAL UNIQUE index scoped to active-stage rows so the
-- constraint only enforces dedupe of in-flight runs. Completed /
-- failed / cancelled runs with the same idempotency_key (which can
-- happen across the 5-minute idempotency window) don't conflict.
-- Number 144 was chosen after checking origin/main plus open PRs:
-- origin/main currently reaches 142, while open PRs touch 068, 097,
-- and 143. The enqueue path uses conflict inference, not
-- ON CONSTRAINT, because partial unique indexes are not valid named
-- constraint targets in Postgres.

-- Idempotent — safe to re-run.
DROP INDEX IF EXISTS uniq_merchant_audit_runs_active_idempotency_key;

CREATE UNIQUE INDEX IF NOT EXISTS
  uniq_merchant_audit_runs_active_idempotency_key
  ON merchant_audit_runs (merchant_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL
    AND stage = ANY(ARRAY[
      'queued'::text, 'discovering'::text, 'probing'::text,
      'scoring'::text, 'materializing'::text, 'verifying'::text
    ]);
