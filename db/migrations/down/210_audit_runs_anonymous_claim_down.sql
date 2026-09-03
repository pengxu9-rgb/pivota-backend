-- Rollback for 210. NOT unconditionally safe, by nature: re-adding NOT NULL
-- fails if any unclaimed run exists, and that is the correct behaviour —
-- silently deleting a visitor's audit to satisfy a constraint would be worse
-- than a failed rollback.
--
-- To roll back deliberately, decide what happens to unclaimed rows FIRST:
--   SELECT count(*) FROM merchant_audit_runs WHERE merchant_id IS NULL;
-- then either let them be claimed, or delete them knowingly, then run this.

DROP INDEX IF EXISTS idx_merchant_audit_runs_unclaimed;

ALTER TABLE merchant_audit_runs
  DROP COLUMN IF EXISTS merchant_claimed_at;

ALTER TABLE merchant_audit_runs
  ALTER COLUMN merchant_id SET NOT NULL;
