-- PR-4a: persist executor agent runs.
--
-- Each row records ONE invocation of an executor agent (e.g.
-- gsc_url_submission_loop, sitemap_freshness_monitor, content_brief_generator).
-- The executor pattern moves Pivota from "advisory action ladder" to
-- "we shipped the fix overnight."
--
-- Schema designed to be agent-agnostic:
--   - agent_name identifies which executor ran
--   - parent_audit_run_id links to merchant_audit_runs / competitor_audit_runs
--     (NULL for cron-only runs not triggered by an audit)
--   - merchant_id for trend / per-merchant filtering
--   - status tracks lifecycle: queued → running → succeeded | failed | skipped
--   - evidence_jsonb stores agent-specific evidence (URLs submitted,
--     sitemap diff, content brief markdown, etc.)
--   - error_message for failed runs
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS executor_runs (
  run_id              UUID PRIMARY KEY,
  agent_name          TEXT NOT NULL,
  merchant_id         TEXT NULL,
  parent_audit_run_id UUID NULL,
  requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at        TIMESTAMPTZ NULL,
  status              TEXT NOT NULL,                       -- queued | running | succeeded | failed | skipped
  evidence_jsonb      JSONB NULL,
  error_message       TEXT NULL
);

-- "Show me recent runs of this agent" — drives ops dashboards.
CREATE INDEX IF NOT EXISTS idx_executor_runs_agent_recent
    ON executor_runs (agent_name, requested_at DESC);

-- "Show me agent activity for this merchant" — drives the merchant_view
-- "what Pivota did for you this week" panel.
CREATE INDEX IF NOT EXISTS idx_executor_runs_merchant_recent
    ON executor_runs (merchant_id, requested_at DESC)
    WHERE merchant_id IS NOT NULL;

-- "Show me agent runs triggered by this audit" — joins to merchant_audit_runs
-- so the audit detail view can render "we ran 3 fixes after this audit."
CREATE INDEX IF NOT EXISTS idx_executor_runs_parent_audit
    ON executor_runs (parent_audit_run_id, requested_at DESC)
    WHERE parent_audit_run_id IS NOT NULL;
