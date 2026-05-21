-- 116_extend_agent_payouts_monetization.sql
-- Extend agent_payouts for channel partner settlement, approval, and clawback flows.

ALTER TABLE IF EXISTS agent_payouts
  ADD COLUMN IF NOT EXISTS payee_type TEXT NOT NULL DEFAULT 'agent',
  ADD COLUMN IF NOT EXISTS payee_id BIGINT,
  ADD COLUMN IF NOT EXISTS comp_config_version INTEGER,
  ADD COLUMN IF NOT EXISTS snapshot_id BIGINT,
  ADD COLUMN IF NOT EXISTS billing_run_id BIGINT,
  ADD COLUMN IF NOT EXISTS subsidy_cap_remaining_cents BIGINT,
  ADD COLUMN IF NOT EXISTS clawback_amount_cents BIGINT NOT NULL DEFAULT 0;

ALTER TABLE IF EXISTS agent_payouts
  ALTER COLUMN agent_id DROP NOT NULL;

ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS agent_payouts_status_check;
ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS check_agent_payouts_status;
ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS ck_agent_payouts_status;
ALTER TABLE IF EXISTS agent_payouts
  ADD CONSTRAINT ck_agent_payouts_status
  CHECK (
    status IN (
      'pending',
      'uploaded',
      'paid',
      'approved',
      'rejected',
      'failed',
      'reversed',
      'clawed_back',
      'clawback_pending'
    )
  );

ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS ck_agent_payouts_payee_type;
ALTER TABLE IF EXISTS agent_payouts
  ADD CONSTRAINT ck_agent_payouts_payee_type
  CHECK (payee_type IN ('agent', 'channel_partner'));

ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS ck_agent_payouts_payee_shape;
ALTER TABLE IF EXISTS agent_payouts
  ADD CONSTRAINT ck_agent_payouts_payee_shape
  CHECK (
    (payee_type = 'agent' AND agent_id IS NOT NULL)
    OR
    (payee_type = 'channel_partner' AND payee_id IS NOT NULL)
  );

ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS ck_agent_payouts_comp_config_version;
ALTER TABLE IF EXISTS agent_payouts
  ADD CONSTRAINT ck_agent_payouts_comp_config_version
  CHECK (comp_config_version IS NULL OR comp_config_version > 0);

ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS ck_agent_payouts_subsidy_cap_remaining;
ALTER TABLE IF EXISTS agent_payouts
  ADD CONSTRAINT ck_agent_payouts_subsidy_cap_remaining
  CHECK (subsidy_cap_remaining_cents IS NULL OR subsidy_cap_remaining_cents >= 0);

ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS ck_agent_payouts_clawback_amount;
ALTER TABLE IF EXISTS agent_payouts
  ADD CONSTRAINT ck_agent_payouts_clawback_amount
  CHECK (clawback_amount_cents >= 0);

ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS agent_payouts_snapshot_id_fkey;
ALTER TABLE IF EXISTS agent_payouts
  ADD CONSTRAINT agent_payouts_snapshot_id_fkey
  FOREIGN KEY (snapshot_id) REFERENCES settlement_snapshots(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS agent_payouts
  DROP CONSTRAINT IF EXISTS agent_payouts_billing_run_id_fkey;
ALTER TABLE IF EXISTS agent_payouts
  ADD CONSTRAINT agent_payouts_billing_run_id_fkey
  FOREIGN KEY (billing_run_id) REFERENCES billing_runs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_agent_payouts_payee
  ON agent_payouts(payee_type, payee_id);
CREATE INDEX IF NOT EXISTS idx_agent_payouts_snapshot_id
  ON agent_payouts(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_agent_payouts_billing_run_id
  ON agent_payouts(billing_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_payouts_status
  ON agent_payouts(status);

COMMENT ON COLUMN agent_payouts.payee_type IS 'Payout recipient type: agent or channel_partner';
COMMENT ON COLUMN agent_payouts.payee_id IS 'Generic payee ID for channel partner payouts';
COMMENT ON COLUMN agent_payouts.snapshot_id IS 'Settlement snapshot used to compute this payout';
COMMENT ON COLUMN agent_payouts.billing_run_id IS 'Billing run that produced this payout';
COMMENT ON COLUMN agent_payouts.clawback_amount_cents IS 'Clawback amount applied to this payout, in cents';
COMMENT ON COLUMN agent_payouts.status IS 'Expanded monetization v1.3 payout lifecycle status';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS agent_payouts DROP CONSTRAINT IF EXISTS agent_payouts_billing_run_id_fkey;
-- ALTER TABLE IF EXISTS agent_payouts DROP CONSTRAINT IF EXISTS agent_payouts_snapshot_id_fkey;
-- ALTER TABLE IF EXISTS agent_payouts DROP CONSTRAINT IF EXISTS ck_agent_payouts_clawback_amount;
-- ALTER TABLE IF EXISTS agent_payouts DROP CONSTRAINT IF EXISTS ck_agent_payouts_subsidy_cap_remaining;
-- ALTER TABLE IF EXISTS agent_payouts DROP CONSTRAINT IF EXISTS ck_agent_payouts_comp_config_version;
-- ALTER TABLE IF EXISTS agent_payouts DROP CONSTRAINT IF EXISTS ck_agent_payouts_payee_shape;
-- ALTER TABLE IF EXISTS agent_payouts DROP CONSTRAINT IF EXISTS ck_agent_payouts_payee_type;
-- ALTER TABLE IF EXISTS agent_payouts DROP CONSTRAINT IF EXISTS ck_agent_payouts_status;
-- ALTER TABLE IF EXISTS agent_payouts ADD CONSTRAINT agent_payouts_status_check CHECK (status IN ('pending', 'uploaded', 'paid'));
-- ALTER TABLE IF EXISTS agent_payouts ALTER COLUMN agent_id SET NOT NULL;
-- ALTER TABLE IF EXISTS agent_payouts DROP COLUMN IF EXISTS clawback_amount_cents;
-- ALTER TABLE IF EXISTS agent_payouts DROP COLUMN IF EXISTS subsidy_cap_remaining_cents;
-- ALTER TABLE IF EXISTS agent_payouts DROP COLUMN IF EXISTS billing_run_id;
-- ALTER TABLE IF EXISTS agent_payouts DROP COLUMN IF EXISTS snapshot_id;
-- ALTER TABLE IF EXISTS agent_payouts DROP COLUMN IF EXISTS comp_config_version;
-- ALTER TABLE IF EXISTS agent_payouts DROP COLUMN IF EXISTS payee_id;
-- ALTER TABLE IF EXISTS agent_payouts DROP COLUMN IF EXISTS payee_type;
