-- 082_seed_data_proposals_allow_no_change.sql
--
-- Follow-up to migration 081. The writer service from PR #426
-- (services/seed_data_writer.upsert_seed_data) returns 'no_change' as
-- a valid WriteResult.status when a proposal's lockable fields are
-- already at parity with the live row. But the CHECK constraint
-- shipped in 081 only allows: pending, approved, rejected, merged,
-- superseded. Codex hit this 2026-05-12 trying to apply a recovery
-- proposal that only updated non-lockable fields (URL, variant
-- images): the proposal-row INSERT failed before any seed_data write,
-- and codex correctly stopped instead of bypassing.
--
-- Two issues in one chain:
--
--   1. The CHECK constraint omitted 'no_change' — fixed here by
--      dropping + re-adding the constraint with the extra value.
--      This migration only fixes prod's installed schema; the inline
--      081 source is patched in the same PR so fresh installs get the
--      right shape from the start.
--
--   2. The writer service was also marking proposals as 'no_change'
--      in cases where non-lockable fields (price, images, URL) HAD
--      changed — those should be 'merged'. That logic bug is fixed in
--      services/seed_data_writer.py in the same PR. Schema fix and
--      writer fix ship together so the constraint and the contract
--      stay aligned.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT. Postgres
-- auto-names the original constraint `seed_data_proposals_status_check`
-- because the column is `status` and the inline CHECK had no explicit
-- name. If you ever see a different name in your DB (e.g. because of a
-- manual rename), update the DROP statement accordingly.

ALTER TABLE seed_data_proposals
  DROP CONSTRAINT IF EXISTS seed_data_proposals_status_check;

ALTER TABLE seed_data_proposals
  ADD CONSTRAINT seed_data_proposals_status_check
  CHECK (status IN (
    'pending',     -- created, awaiting service eval
    'approved',    -- service decided this should merge; not yet applied
    'rejected',    -- service rejected (lock violation, lower quality, audit fail)
    'merged',      -- applied to external_product_seeds
    'superseded',  -- newer proposal arrived for the same fields before this one merged
    'no_change'    -- writer detected proposal is identical to current state on every
                   -- lockable field AND no non-lockable change either. Kept as a
                   -- forensic record: "codex proposed this, it was already correct."
                   -- Added 2026-05-12 (mig 082) to align with writer service contract.
  ));
