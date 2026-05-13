-- Q-P0-2 / Q-P1-4: cross-audit task supersession.
--
-- Pre-fix, materializing tasks for a new audit run created fresh
-- merchant_tasks rows with no link to the prior audit's identical
-- pending tasks. The GET /tasks endpoint then returned both, and
-- the merchant saw stale tasks from 6+ audit runs as if they were
-- current (the Winona prod artifact showed 33 tasks across 6 runs).
--
-- This migration adds the `superseded_by_task_id` pointer so the
-- task queue service can:
--   1. Mark prior pending tasks with the same canonical identity
--      (merchant + lever + normalized title + target_host +
--      product_key) as `status='superseded'` when a newer audit
--      emits the same action.
--   2. Preserve audit-trail visibility — operators can still query
--      `?status_filter=superseded` to see why a task disappeared.
--
-- The new `superseded` status is enforced via `merchant_tasks.status`
-- (TEXT NOT NULL) — no CHECK constraint to validate; the service
-- layer's VALID_STATUSES gates writes.
--
-- Idempotent — safe to re-run.

ALTER TABLE merchant_tasks
    ADD COLUMN IF NOT EXISTS superseded_by_task_id UUID NULL;

-- Partial index to keep the active-task lookup fast even as
-- superseded rows accumulate. Most lookups filter on
-- status IN ('pending', 'in_progress'), so we add a partial covering
-- the OPEN states; the existing idx_merchant_tasks_open already covers
-- the multi-column hot path. This new index makes the
-- "find pending tasks with same identity for supersession" lookup
-- cheap (the service calls this on every new materialization).
CREATE INDEX IF NOT EXISTS idx_merchant_tasks_identity_pending
    ON merchant_tasks (merchant_id, lever, title)
    WHERE status = 'pending';

-- Index for traversing the supersession chain (audit history).
CREATE INDEX IF NOT EXISTS idx_merchant_tasks_superseded_by
    ON merchant_tasks (superseded_by_task_id)
    WHERE superseded_by_task_id IS NOT NULL;
