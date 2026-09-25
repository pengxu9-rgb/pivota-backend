-- ADR-025 D5: an agent's share on a channel-partner line is taken AFTER the partner's cut
-- (decision 2026-09-23). The cut is spread from what settlement actually paid the partner(s) from
-- the merchant's take in the billing run. Each ledger row records the amount paid, the merchant's
-- billed total in the run, and the line's cut, so it can be re-derived by hand.
--
-- The same columns are declared in db/agent_share.py, and db/schema_guard.py adds them at startup:
-- create_all never alters an existing table, and prod does not apply numbered migrations.

ALTER TABLE IF EXISTS agent_share_ledger
  ADD COLUMN IF NOT EXISTS partner_settled_minor BIGINT,
  ADD COLUMN IF NOT EXISTS merchant_billed_minor BIGINT,
  ADD COLUMN IF NOT EXISTS partner_cut_minor BIGINT;

-- Partner settlement completion per billing run. Agent share accrual deducts what partners were
-- paid only from a run whose settlement completed, so a partner attributed afterwards can never
-- re-price an accrued line. Also declared in db/agent_share.py (create_all creates it at startup).
CREATE TABLE IF NOT EXISTS partner_settlement_completions (
  billing_run_id BIGINT PRIMARY KEY,
  partner_ids JSONB NOT NULL,
  engine VARCHAR(8) NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
