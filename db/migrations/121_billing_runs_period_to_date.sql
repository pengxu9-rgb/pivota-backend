-- 121_billing_runs_period_to_date.sql
-- Convert billing_runs.period_start / period_end from TIMESTAMPTZ to DATE.
--
-- Migration 120 fixed invoices.billing_period_start/end (TIMESTAMPTZ → DATE)
-- to stop the cron from misattributing by one day under session-TZ != UTC.
-- billing_runs.period_start/end have the same TIMESTAMPTZ shape and the
-- same timezone-drift exposure — visible in Step 6 where a date(2026,3,1)
-- argument was stored as 2026-02-28 16:00:00+00 (Asia/Shanghai midnight =
-- 2026-02-28 16:00 UTC). The cross-table inconsistency between
-- billing_runs.period_start (TIMESTAMPTZ) and gmv_attribution_daily.date
-- (DATE) is a future-bug magnet for any JOIN on period boundaries.
--
-- USING ::date drops the time-of-day component. For empty rows = no-op;
-- for existing rows uses session timezone. Run during low-traffic window.
--
-- The CHECK constraint `ck_billing_runs_period_order` (period_end >
-- period_start) continues to satisfy because DATE > comparison is well-defined.

ALTER TABLE IF EXISTS billing_runs
  ALTER COLUMN period_start TYPE DATE USING period_start::date,
  ALTER COLUMN period_end   TYPE DATE USING period_end::date;

COMMENT ON COLUMN billing_runs.period_start IS 'Calendar-day start of the billing cycle covered by this run (DATE, not TIMESTAMPTZ — aligned with invoices.billing_period_start)';
COMMENT ON COLUMN billing_runs.period_end   IS 'Calendar-day end of the billing cycle covered by this run (DATE, not TIMESTAMPTZ — aligned with invoices.billing_period_end)';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS billing_runs
--   ALTER COLUMN period_start TYPE TIMESTAMPTZ USING period_start::timestamptz,
--   ALTER COLUMN period_end   TYPE TIMESTAMPTZ USING period_end::timestamptz;
