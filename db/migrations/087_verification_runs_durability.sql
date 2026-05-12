-- P5.1: verification_runs work-queue durability.
--
-- P4.1 created the verification_runs table as a Phase 5 scaffold
-- (status / evidence_jsonb / verifier_id columns). P5.1 makes it a
-- durable work queue with the same claim/lease/retry pattern P3.1
-- used for executor_runs.
--
-- New stage values are a SUPERSET of the existing status values:
--   pending → claimed → succeeded
--                      → failed → (re-enqueue) → claimed → ...
--                      → exhausted_retries
--                      → blocked (when upstream system is unavailable —
--                                 NOT a soft failure; don't retry)
--
-- The existing `status` column already had 'pending' / 'running' /
-- 'succeeded' / 'failed' / 'blocked' per P4.1. P5.1 reuses 'status'
-- as the stage column (no rename — 'status' is fine here, mirrors
-- the verifier state). Adds the lease + retry columns.
--
-- Idempotent — safe to re-run.

ALTER TABLE verification_runs
  ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT;
COMMENT ON COLUMN verification_runs.claimed_by_worker IS
  'Worker process id (host_pid_uuid8) that owns the lease. NULL = unclaimed.';

ALTER TABLE verification_runs
  ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;
COMMENT ON COLUMN verification_runs.claimed_until IS
  'Lease expiry. Default 120s — verifiers are short HTTP fetches. Worker extends only for the 30-day-delayed citation_movement verifier.';

ALTER TABLE verification_runs
  ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE verification_runs
  ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 2;
COMMENT ON COLUMN verification_runs.max_retries IS
  'Per-verifier retry budget. Default 2 (1 initial + 1 retry). Verifiers either find the evidence or they don''t — retries help with transient HTTP failures but won''t fix missing PDP rendering or GSC indexing gaps.';

ALTER TABLE verification_runs
  ADD COLUMN IF NOT EXISTS not_before TIMESTAMPTZ;
COMMENT ON COLUMN verification_runs.not_before IS
  'When the worker is allowed to claim this row. NULL = claim immediately. Used by the public_llm_citation_movement verifier which runs 30 days post-audit (P5.6).';

ALTER TABLE verification_runs
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
COMMENT ON COLUMN verification_runs.idempotency_key IS
  'sha256(audit_run_id|verifier_id|product_key). Prevents re-enqueueing duplicate verifier work for the same audit (e.g., audit completion firing the enqueue twice).';

-- =======================================================================
-- Indexes
-- =======================================================================

-- Worker pull query: WHERE status IN ('pending', 'claimed')
--                     AND (claimed_until IS NULL OR claimed_until < NOW())
--                     AND (not_before IS NULL OR not_before < NOW())
--                   ORDER BY created_at LIMIT 1
CREATE INDEX IF NOT EXISTS idx_verification_runs_worker_pull
  ON verification_runs (status, claimed_until, not_before, created_at)
  WHERE status IN ('pending', 'claimed');

-- Idempotency dedupe lookup.
CREATE INDEX IF NOT EXISTS idx_verification_runs_idempotency
  ON verification_runs (idempotency_key)
  WHERE idempotency_key IS NOT NULL
    AND status IN ('pending', 'claimed');
