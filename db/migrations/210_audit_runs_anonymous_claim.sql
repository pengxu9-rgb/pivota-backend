-- C3: anonymous audit runs, claimed at conversion.
--
-- The public funnel (routes/store_audit_public_intake.py) starts a run for a
-- visitor who has not registered. Until now merchant_audit_runs.merchant_id
-- was NOT NULL, so no such row could exist and the marketing funnel could only
-- ever show a protocol teaser rather than the audit itself.
--
-- Conversion claims the SAME row via db.merchant_audit_runs
-- .claim_audit_run_for_merchant — one guarded UPDATE, never a copy. The
-- `merchant_id IS NULL` guard in that statement is what makes it a claim and
-- not a takeover of someone else's run.
--
-- Read safety: an unclaimed row grants nothing. Every ownership check compares
-- `row.get("merchant_id") != merchant_id` where the right-hand side comes from
-- get_current_merchant, which raises 401 on a falsy claim — so it can never be
-- None, and None != "<real id>" rejects.
--
-- NOT named claimed_at: claimed_by_worker / claimed_until on this same table
-- are the worker lease, and a bare claimed_at would read as part of it.

ALTER TABLE merchant_audit_runs
  ALTER COLUMN merchant_id DROP NOT NULL;

ALTER TABLE merchant_audit_runs
  ADD COLUMN IF NOT EXISTS merchant_claimed_at TIMESTAMPTZ NULL;

-- The funnel's only sweep is "unclaimed runs, newest first".
CREATE INDEX IF NOT EXISTS idx_merchant_audit_runs_unclaimed
  ON merchant_audit_runs (requested_at DESC)
  WHERE merchant_id IS NULL;
