-- 120_invoices_billing_period_to_date.sql
-- Convert invoices.billing_period_start / billing_period_end from TIMESTAMPTZ to DATE.
--
-- Migration 113 (was 079) created these columns as TIMESTAMPTZ, but v1.3 §1.3 treats
-- billing periods as calendar-day-bounded — there is no time-of-day component to a
-- monthly Pivota billing cycle. Storing as TIMESTAMPTZ caused timezone drift:
-- a service running in UTC writing `period_start = 2026-05-01` ended up as
-- `2026-04-30 16:00 UTC` for an Asia/Shanghai input, and any query comparing
-- to a calendar date misattributed by one day. Surfaced by the Test Clock harness.
--
-- USING ... ::date drops the time-of-day component, converting to the local DATE.
-- For empty tables this is a no-op data-wise; for tables with rows, the conversion
-- uses the session timezone. Run during low-traffic window if `invoices` has data.
--
-- CHECK constraint `ck_invoices_billing_period_order` still satisfies because
-- DATE comparisons work the same way as TIMESTAMPTZ comparisons for ordering.

ALTER TABLE IF EXISTS invoices
  ALTER COLUMN billing_period_start TYPE DATE USING billing_period_start::date,
  ALTER COLUMN billing_period_end   TYPE DATE USING billing_period_end::date;

COMMENT ON COLUMN invoices.billing_period_start IS 'Calendar-day start of the monthly billing period (DATE, not TIMESTAMPTZ — v1.3 §1.3)';
COMMENT ON COLUMN invoices.billing_period_end   IS 'Calendar-day end of the monthly billing period (DATE, not TIMESTAMPTZ — v1.3 §1.3)';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS invoices
--   ALTER COLUMN billing_period_start TYPE TIMESTAMPTZ USING billing_period_start::timestamptz,
--   ALTER COLUMN billing_period_end   TYPE TIMESTAMPTZ USING billing_period_end::timestamptz;
