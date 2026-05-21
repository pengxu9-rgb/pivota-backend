-- 114_settlement_snapshots.sql
-- Immutable partner settlement computation snapshots.

CREATE TABLE IF NOT EXISTS settlement_snapshots (
  id BIGSERIAL PRIMARY KEY,
  billing_run_id BIGINT NOT NULL REFERENCES billing_runs(id) ON DELETE RESTRICT,
  channel_partner_id BIGINT NOT NULL REFERENCES channel_partners(id) ON DELETE RESTRICT,
  snapshot_payload_jsonb JSONB NOT NULL,
  computed_comp_cents BIGINT NOT NULL,
  subsidy_cap_remaining_at_snapshot BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_settlement_snapshots_computed_comp_nonnegative CHECK (computed_comp_cents >= 0),
  CONSTRAINT ck_settlement_snapshots_subsidy_cap_nonnegative CHECK (
    subsidy_cap_remaining_at_snapshot IS NULL
    OR subsidy_cap_remaining_at_snapshot >= 0
  )
);

CREATE INDEX IF NOT EXISTS idx_settlement_snapshots_billing_run_id
  ON settlement_snapshots(billing_run_id);
CREATE INDEX IF NOT EXISTS idx_settlement_snapshots_channel_partner_id
  ON settlement_snapshots(channel_partner_id);
CREATE INDEX IF NOT EXISTS idx_settlement_snapshots_created_at
  ON settlement_snapshots(created_at DESC);

CREATE OR REPLACE FUNCTION prevent_monetization_append_only_mutation()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'monetization append-only table % does not allow UPDATE or DELETE', TG_TABLE_NAME;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_settlement_snapshots_append_only ON settlement_snapshots;
CREATE TRIGGER trg_settlement_snapshots_append_only
  BEFORE UPDATE OR DELETE ON settlement_snapshots
  FOR EACH ROW EXECUTE PROCEDURE prevent_monetization_append_only_mutation();

COMMENT ON TABLE settlement_snapshots IS 'Monetization v1.3: immutable append-only partner settlement snapshots; UPDATE and DELETE are blocked by trigger';
COMMENT ON COLUMN settlement_snapshots.snapshot_payload_jsonb IS 'Point-in-time input and output payload for audit replay';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_settlement_snapshots_append_only ON settlement_snapshots;
-- DROP TABLE IF EXISTS settlement_snapshots;
