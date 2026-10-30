-- Reverse of 252_reap_agentic_enrollments_one_pending.sql.
--
-- WHAT THIS DESTROYS: nothing but the index. No row is touched.
--
-- WHAT IT RE-OPENS: two purchases of one buyer in one poll tick can again each mint a pending
-- enrollment (two hosted links to one buyer). The purchase service still reconciles every
-- pending row, so nothing is stranded; the duplicate is simply no longer prevented.

DROP INDEX IF EXISTS uq_reap_agentic_enrollments_one_pending;
