-- P0-3: DB-enforced audit idempotency. Closes the check-then-insert
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
--
-- Naming this as an explicit CONSTRAINT (not just an index) so the
-- enqueue path can reference it directly with
-- `ON CONFLICT ON CONSTRAINT uniq_merchant_audit_runs_active_idempotency_key`.
-- Postgres treats partial unique indexes as constraints only when
-- declared via ADD CONSTRAINT or CREATE UNIQUE INDEX where the
-- index name matches. We use CREATE UNIQUE INDEX here; the matching
-- `ON CONFLICT ON CONSTRAINT <indexname>` syntax requires the index
-- to be the unique enforcement source.

-- Idempotent — safe to re-run.
CREATE UNIQUE INDEX IF NOT EXISTS
  uniq_merchant_audit_runs_active_idempotency_key
  ON merchant_audit_runs (idempotency_key)
  WHERE idempotency_key IS NOT NULL
    AND stage IN (
      'queued', 'discovering', 'probing', 'scoring',
      'materializing', 'verifying'
    );
