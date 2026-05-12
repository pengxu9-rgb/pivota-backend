-- P2.1: async audit run lifecycle (Phase 2 schema foundation)
--
-- Today's merchant_audit_runs.status uses 3 values (running |
-- succeeded | failed) and the route blocks for the entire run. Phase
-- 2 introduces an async lifecycle where:
--   POST /api/audits creates a row with stage='queued', returns id
--   immediately; a background worker advances stage through
--   discovering → probing → scoring → materializing → verifying →
--   completed. GET /api/audits/{id} returns stage + partial result.
--
-- This migration adds the columns Phase 2 endpoints + worker need.
-- Old `status` column is preserved during the dual-key window; the
-- new `stage` column is the canonical state machine. Phase 4 cleanup
-- will retire `status` once all consumers read `stage`.
--
-- Idempotent — safe to re-run.

-- =======================================================================
-- merchant_audit_runs — owning table for the async lifecycle
-- =======================================================================

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'completed';
COMMENT ON COLUMN merchant_audit_runs.stage IS
  'Async lifecycle state machine: queued | discovering | probing | scoring | materializing | verifying | completed | failed | cancelled. Default completed so existing rows backfill cleanly.';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS stage_updated_at TIMESTAMPTZ;
COMMENT ON COLUMN merchant_audit_runs.stage_updated_at IS
  'When `stage` last changed. Powers stale-lease reclaim + stage-duration metrics.';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS partial_result_jsonb JSONB;
COMMENT ON COLUMN merchant_audit_runs.partial_result_jsonb IS
  'Stage-by-stage payload populated as each stage completes. Renderer can show progressive UI without waiting for the full audit.';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS error_jsonb JSONB;
COMMENT ON COLUMN merchant_audit_runs.error_jsonb IS
  'On stage=failed: {stage, message, traceback_truncated}. Distinct from `error_message` (which is a free-form string from the legacy synchronous path).';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS cost_summary_jsonb JSONB;
COMMENT ON COLUMN merchant_audit_runs.cost_summary_jsonb IS
  'Aggregated from llm_probe_runs at stage=completed. Shape: {llm_calls, total_input_tokens, total_output_tokens, estimated_cost_usd, providers: [{provider, calls, cost_usd}]}.';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
COMMENT ON COLUMN merchant_audit_runs.idempotency_key IS
  'sha256(subject_type|merchant_id|sorted(product_keys)|window_floor) per services/idempotency.py. POST /api/audits dedupes against in-flight runs with the same key.';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
COMMENT ON COLUMN merchant_audit_runs.cancelled_at IS
  'Set when caller hits POST /api/audits/{id}/cancel. Worker checks before each stage transition.';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS requested_by_user_id TEXT;
COMMENT ON COLUMN merchant_audit_runs.requested_by_user_id IS
  'Who submitted the audit. Used for cost accounting + per-user rate limits.';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS subject_type TEXT NOT NULL DEFAULT 'merchant';
COMMENT ON COLUMN merchant_audit_runs.subject_type IS
  'merchant (self-audit) | cold_start (BD prospect). Lets the worker pick the right pipeline based on subject.';

-- =======================================================================
-- Worker lease + retry columns (claim/release pattern from Phase 2)
-- =======================================================================

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT;
COMMENT ON COLUMN merchant_audit_runs.claimed_by_worker IS
  'Worker process id that currently owns this run. NULL = unclaimed; a value + claimed_until in past = stale lease (reclaimable).';

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;
COMMENT ON COLUMN merchant_audit_runs.claimed_until IS
  'When the current claim expires. Default lease = 600s. Worker MUST extend before completing a long stage.';

-- =======================================================================
-- Indexes
-- =======================================================================

-- Worker pull query: WHERE stage = 'queued' AND (claimed_until IS NULL
-- OR claimed_until < NOW()) ORDER BY requested_at LIMIT 1
CREATE INDEX IF NOT EXISTS idx_merchant_audit_runs_worker_pull
  ON merchant_audit_runs (stage, claimed_until, requested_at)
  WHERE stage IN ('queued', 'discovering', 'probing', 'scoring',
                  'materializing', 'verifying');

-- Idempotency lookup: SELECT run_id FROM merchant_audit_runs
-- WHERE idempotency_key = $1 AND stage NOT IN ('completed', 'failed', 'cancelled')
-- Partial index keeps it small (most rows don't have an idempotency_key
-- set yet, and lookups only care about in-flight runs).
CREATE INDEX IF NOT EXISTS idx_merchant_audit_runs_idempotency
  ON merchant_audit_runs (idempotency_key)
  WHERE idempotency_key IS NOT NULL
    AND stage IN ('queued', 'discovering', 'probing', 'scoring',
                  'materializing', 'verifying');

-- =======================================================================
-- Backfill — existing rows are completed (legacy synchronous path)
-- =======================================================================

UPDATE merchant_audit_runs
   SET stage = CASE
                  WHEN status = 'succeeded' THEN 'completed'
                  WHEN status = 'failed'    THEN 'failed'
                  WHEN status = 'running'   THEN 'queued'
                  ELSE 'completed'
                END,
       stage_updated_at = COALESCE(stage_updated_at, completed_at, requested_at)
 WHERE stage IS NULL OR stage = 'completed';
-- Note: this UPDATE is idempotent — re-running on a row already at
-- 'completed' with stage_updated_at set is a no-op against the
-- WHERE clause's first iteration but always-safe due to COALESCE.

-- =======================================================================
-- competitor_audit_runs — same lifecycle columns
-- =======================================================================
-- Cohort competitors run through the same async pipeline. Keep the
-- column set in lockstep so the worker can treat both tables uniformly.

ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS stage_updated_at TIMESTAMPTZ;
ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS partial_result_jsonb JSONB;
ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS error_jsonb JSONB;
ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS cost_summary_jsonb JSONB;
ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT;
ALTER TABLE competitor_audit_runs
  ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_competitor_audit_runs_worker_pull
  ON competitor_audit_runs (stage, claimed_until, requested_at)
  WHERE stage IN ('queued', 'discovering', 'probing', 'scoring',
                  'materializing', 'verifying');

UPDATE competitor_audit_runs
   SET stage = CASE
                  WHEN status = 'succeeded' THEN 'completed'
                  WHEN status = 'failed'    THEN 'failed'
                  WHEN status = 'running'   THEN 'queued'
                  ELSE 'completed'
                END,
       stage_updated_at = COALESCE(stage_updated_at, completed_at, requested_at)
 WHERE stage IS NULL OR stage = 'completed';
