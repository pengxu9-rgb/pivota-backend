-- P3.1: durable executor_runs work queue.
--
-- Today executor agents fire as `asyncio.create_task(dispatch_agents(...))`
-- in services/executor_agents/dispatcher.py — fire-and-forget.
-- On worker crash mid-execution, in-flight executor work is lost
-- (the audit_run that triggered them already completed; nothing
-- restarts the executors). This migration makes executor_runs a
-- durable work queue with the same claim/lease/retry pattern P2.1
-- introduced for audit_runs.
--
-- New stage values (canonical state machine):
--   queued    — enqueued by dispatcher; waiting for worker to claim
--   claimed   — worker is mid-execution
--   succeeded — terminal, executor agent completed cleanly
--   failed    — terminal, agent raised an exception (will retry up
--               to max_retries times via re-enqueue at the worker
--               level; once retry_count == max_retries this is a
--               permanent failure)
--   exhausted_retries — terminal, retry budget consumed
--
-- Legacy `status` column ("running"/"succeeded"/"failed"/"skipped")
-- preserved during the dual-key window. Phase 4 cleanup retires it.
--
-- Idempotent — safe to re-run.

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'succeeded';
COMMENT ON COLUMN executor_runs.stage IS
  'Async work-queue stage: queued | claimed | succeeded | failed | exhausted_retries. Default succeeded so existing rows backfill cleanly.';

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS stage_updated_at TIMESTAMPTZ;

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT;
COMMENT ON COLUMN executor_runs.claimed_by_worker IS
  'Worker process id (host_pid_uuid8) that owns the lease. NULL = unclaimed.';

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;
COMMENT ON COLUMN executor_runs.claimed_until IS
  'Lease expiry. Default 300s — most agents complete in seconds. Worker extends for long-running agents (sitemap fetches, GSC URL submissions).';

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
COMMENT ON COLUMN executor_runs.retry_count IS
  'Number of times this row has been re-claimed after a failed attempt. Worker increments before each retry. Compared to max_retries to decide whether to mark exhausted_retries.';

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 3;
COMMENT ON COLUMN executor_runs.max_retries IS
  'Per-agent retry budget. Default 3 — most transient failures (rate limit, network blip) recover within 3 attempts. Agents that have a known-bad cost-on-retry can opt for max_retries=0.';

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS error_jsonb JSONB;
COMMENT ON COLUMN executor_runs.error_jsonb IS
  'On stage=failed or exhausted_retries: {message, traceback_truncated, attempt_history: [{attempt, error}, ...]}. Distinct from `error_message` (legacy free-form string).';

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS payload_jsonb JSONB;
COMMENT ON COLUMN executor_runs.payload_jsonb IS
  'Per-agent input parameters needed to re-run. Worker calls the agent with these on claim. Shape depends on agent_name (e.g., GscUrlSubmissionAgent needs the merchant URL set; SitemapFreshnessMonitor needs the merchant_id only).';

ALTER TABLE executor_runs
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
COMMENT ON COLUMN executor_runs.idempotency_key IS
  'Optional per-(agent_name, merchant_id, parent_audit_run_id) dedupe key. Dispatcher computes this so re-running the same audit doesn''t enqueue duplicate executor work.';

-- =======================================================================
-- Indexes
-- =======================================================================

-- Worker pull query: WHERE stage = 'queued' AND (claimed_until IS NULL
-- OR claimed_until < NOW()) ORDER BY requested_at LIMIT 1
CREATE INDEX IF NOT EXISTS idx_executor_runs_worker_pull
  ON executor_runs (stage, claimed_until, requested_at)
  WHERE stage IN ('queued', 'claimed');

-- Idempotency dedupe lookup. Partial — most rows have NULL key.
CREATE INDEX IF NOT EXISTS idx_executor_runs_idempotency
  ON executor_runs (idempotency_key)
  WHERE idempotency_key IS NOT NULL
    AND stage IN ('queued', 'claimed');

-- =======================================================================
-- Backfill — existing rows are succeeded (legacy fire-and-forget path)
-- =======================================================================

UPDATE executor_runs
   SET stage = CASE
                  WHEN status = 'succeeded' THEN 'succeeded'
                  WHEN status = 'failed'    THEN 'failed'
                  WHEN status = 'skipped'   THEN 'succeeded'
                  WHEN status = 'running'   THEN 'queued'
                  ELSE 'succeeded'
                END,
       stage_updated_at = COALESCE(stage_updated_at, completed_at, requested_at)
 WHERE stage IS NULL OR stage = 'succeeded';
