-- Q-P1-5: executor-produced tasks are children of audit action tasks.
--
-- Executor agents can turn an audit recommendation into a concrete
-- artifact, for example a drafted content brief. The artifact remains
-- actionable, but it should render under the audit-emitted parent
-- action instead of as an unrelated sibling task.
--
-- Idempotent -- safe to re-run.

ALTER TABLE merchant_tasks
    ADD COLUMN IF NOT EXISTS parent_task_id UUID NULL;

CREATE INDEX IF NOT EXISTS idx_merchant_tasks_parent_task
    ON merchant_tasks (parent_task_id)
    WHERE parent_task_id IS NOT NULL;
