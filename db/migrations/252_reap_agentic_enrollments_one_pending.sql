-- 252: AT MOST ONE PENDING ENROLLMENT PER BUYER.
--
-- A partial unique index on reap_agentic_enrollments (buyer_ref) WHERE status = 'pending', the
-- pending-row sibling of 224's uq_reap_agentic_enrollments_one_active.
--
-- WHY. The purchase service mints a buyer's enrollment row (its id is the attempt id Reap's
-- idempotency key is built from) only after reconciling the buyer's existing pending row with
-- Reap. Two purchases of ONE buyer, each in 'resolving' in the same poll tick, could both find
-- no pending row and both INSERT one: two rows, two enrollments at Reap, two hosted links to one
-- buyer, and whichever the buyer did NOT finish left pending here while the other was ACTIVE at
-- Reap (review of #2483, P2-1). "One pending row per buyer" had been kept by code alone, and
-- code alone cannot keep it across two workers.
--
-- With the index the second INSERT is refused, and db/reap_agentic_ledger.upsert_pending_enrollment
-- turns that refusal into "use the winner": both purchases share one attempt, so Reap's
-- idempotency gives them one enrollment and one link.
--
-- ADDITIVE AND OPTIONAL FOR CORRECTNESS. Before it is applied the rail is still correct — the
-- service reconciles EVERY pending row of the buyer, oldest first, so a duplicate is resolved on
-- the next purchase rather than stranded — it just cannot PREVENT the duplicate.
--
-- IT FAILS ON A DATABASE THAT ALREADY HOLDS TWO PENDING ROWS FOR ONE buyer_ref. Run the census
-- in docs/runbooks/reap_agentic_purchase.md ("One pending row per buyer") first; resolve any
-- duplicate by reconciling it with Reap (GET the enrollment; ACTIVE -> mark_enrollment_active,
-- otherwise mark_enrollment_dead), never by deleting it.
--
-- Production deploys skip db/migrations/, so the same index is ALSO in
-- db/schema_guard.ensure_required_schema_light, in BOTH dialect branches, each in its own try
-- (a database with duplicates skips it there rather than failing startup).

CREATE UNIQUE INDEX IF NOT EXISTS uq_reap_agentic_enrollments_one_pending
    ON reap_agentic_enrollments (buyer_ref)
    WHERE status = 'pending';
