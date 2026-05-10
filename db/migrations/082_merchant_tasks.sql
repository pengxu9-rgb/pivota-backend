-- PR-6: human task queue.
--
-- Until PR-6, action items in the audit report were advisory text:
-- the merchant reads them, decides whether to act, no system memory
-- of "did they do it." This table converts each audit's action_items
-- (and each executor agent's emission) into a tracked task with
-- lifecycle: pending → in_progress → done | dismissed | failed.
--
-- One row per task. The audit report's action_items[] becomes N
-- merchant_tasks rows when materialize_tasks_from_audit fires.
-- Executor agents that produce work for humans (sitemap diff,
-- content brief) also write rows here.
--
-- Dual assignment surface:
--   - assigned_to_agent: a Pivota executor agent (executor_runs.run_id
--     reference) that owns this task. Set when an executor produced
--     the task as evidence of work it can't fully complete (e.g.
--     content brief generated, but the merchant's content team needs
--     to write the article).
--   - assigned_to_human: a string identifier (email or display name)
--     for the human owner. Free-form; the BD/merchant portal sets
--     based on its own auth context.
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS merchant_tasks (
  task_id              UUID PRIMARY KEY,
  merchant_id          TEXT NOT NULL,
  parent_audit_run_id  UUID NULL,
  source_executor_run_id UUID NULL,
  lever                TEXT NULL,                  -- e.g. 'pivota_integration', 'gsc_integration', 'content_brief'
  severity             TEXT NOT NULL DEFAULT 'medium', -- 'critical' | 'high' | 'medium' | 'low'
  title                TEXT NOT NULL,
  body                 TEXT NULL,
  status               TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'in_progress' | 'done' | 'dismissed' | 'failed'
  assigned_to_agent    TEXT NULL,
  assigned_to_human    TEXT NULL,
  evidence_jsonb       JSONB NULL,                  -- per-task evidence (e.g. content_brief markdown, sitemap diff sample)
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at         TIMESTAMPTZ NULL,
  dismissed_reason     TEXT NULL
);

-- "What's on this merchant's plate right now?" — drives the merchant
-- portal task queue.
CREATE INDEX IF NOT EXISTS idx_merchant_tasks_open
    ON merchant_tasks (merchant_id, status, severity, created_at DESC)
    WHERE status IN ('pending', 'in_progress');

-- Audit drill-down: "what tasks were materialized from this audit?"
CREATE INDEX IF NOT EXISTS idx_merchant_tasks_audit
    ON merchant_tasks (parent_audit_run_id)
    WHERE parent_audit_run_id IS NOT NULL;

-- "Show me tasks an agent created" (cross-agent reporting).
CREATE INDEX IF NOT EXISTS idx_merchant_tasks_executor_source
    ON merchant_tasks (source_executor_run_id)
    WHERE source_executor_run_id IS NOT NULL;
