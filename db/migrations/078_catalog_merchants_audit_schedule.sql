-- PR-1b: opt-in flag for the auto-re-audit cron.
--
-- A merchant with `audit_schedule='weekly'` or `'monthly'` is picked up
-- by jobs/scheduled_audit_job.run_scheduled_audits and re-audited at
-- the documented cadence. `'none'` (default) opts out — manual re-audits
-- only.
--
-- Idempotent — safe to re-run.

ALTER TABLE catalog_merchants
    ADD COLUMN IF NOT EXISTS audit_schedule TEXT NOT NULL DEFAULT 'none';

-- Constraint: only allowed values. Allows null for older drivers, but
-- the column is NOT NULL so the only effective values are these three.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'catalog_merchants_audit_schedule_chk'
    ) THEN
        ALTER TABLE catalog_merchants
            ADD CONSTRAINT catalog_merchants_audit_schedule_chk
            CHECK (audit_schedule IN ('none', 'weekly', 'monthly'));
    END IF;
END $$;

-- Partial index — only the rows the cron actually queries. Avoids
-- bloat on the dominant 'none' rows.
CREATE INDEX IF NOT EXISTS idx_catalog_merchants_audit_schedule_due
    ON catalog_merchants (audit_schedule)
    WHERE audit_schedule != 'none';
