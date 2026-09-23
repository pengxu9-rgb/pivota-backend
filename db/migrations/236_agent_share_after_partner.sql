-- ADR-025 D5: an agent's share on a channel-partner line is taken AFTER the partner's cut
-- (decision 2026-09-23). Each ledger row records the partner's rate and the cut deducted, so it
-- can be re-derived by hand.
--
-- The same columns are declared in db/agent_share.py, and db/schema_guard.py adds them at startup:
-- create_all never alters an existing table, and prod does not apply numbered migrations.

ALTER TABLE IF EXISTS agent_share_ledger
  ADD COLUMN IF NOT EXISTS partner_share_bp INTEGER,
  ADD COLUMN IF NOT EXISTS partner_cut_minor BIGINT;
