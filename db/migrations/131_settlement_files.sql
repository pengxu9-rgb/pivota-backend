-- 131_settlement_files.sql
-- Monthly partner settlement files and settle markers for snapshots.

CREATE TABLE IF NOT EXISTS settlement_files (
  id BIGSERIAL PRIMARY KEY,
  channel_partner_id BIGINT NOT NULL REFERENCES channel_partners(id) ON DELETE RESTRICT,
  calendar_month DATE NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Per-stream gross share cents from settlement_snapshots for this period
  subscription_share_cents BIGINT NOT NULL DEFAULT 0,
  credit_overage_share_cents BIGINT NOT NULL DEFAULT 0,
  gmv_share_cents BIGINT NOT NULL DEFAULT 0,
  -- Clawback total cents (positive = amount being reversed)
  clawback_cents BIGINT NOT NULL DEFAULT 0,
  -- Net before carryover: shares - clawbacks (signed; can be negative)
  net_before_carryover_cents BIGINT NOT NULL DEFAULT 0,
  -- Carryover applied from previous month (signed; usually 0 or negative)
  carryover_applied_cents BIGINT NOT NULL DEFAULT 0,
  -- Transfer amount: max(net_before_carryover + carryover_applied, 0)
  transfer_amount_cents BIGINT NOT NULL DEFAULT 0,
  -- Carryover forward to next month: min(net_before_carryover + carryover_applied, 0)
  -- Stored as a NEGATIVE number (or 0); next month picks it up as carryover_applied
  carryover_forward_cents BIGINT NOT NULL DEFAULT 0,
  -- Source snapshot ids (JSONB array of bigints) for audit lineage
  source_snapshot_ids_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
  transfer_status TEXT NOT NULL DEFAULT 'pending',
  stripe_transfer_id TEXT,
  stripe_transfer_error TEXT,
  transferred_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_settlement_files_partner_month UNIQUE (channel_partner_id, calendar_month),
  CONSTRAINT ck_settlement_files_calendar_month_first
    CHECK (EXTRACT(DAY FROM calendar_month) = 1),
  CONSTRAINT ck_settlement_files_transfer_amount_nonneg
    CHECK (transfer_amount_cents >= 0),
  CONSTRAINT ck_settlement_files_carryover_forward_nonpos
    CHECK (carryover_forward_cents <= 0),
  CONSTRAINT ck_settlement_files_carryover_applied_nonpos
    CHECK (carryover_applied_cents <= 0),
  CONSTRAINT ck_settlement_files_transfer_status
    CHECK (transfer_status IN ('pending', 'transferring', 'transferred', 'failed', 'skipped_negative_net'))
);

CREATE INDEX IF NOT EXISTS idx_settlement_files_partner_month
  ON settlement_files (channel_partner_id, calendar_month DESC);
CREATE INDEX IF NOT EXISTS idx_settlement_files_transfer_status
  ON settlement_files (transfer_status);
CREATE INDEX IF NOT EXISTS idx_settlement_files_pending_partner
  ON settlement_files (channel_partner_id) WHERE transfer_status = 'pending';

DROP TRIGGER IF EXISTS trg_settlement_files_updated_at ON settlement_files;
CREATE TRIGGER trg_settlement_files_updated_at
  BEFORE UPDATE ON settlement_files
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

ALTER TABLE settlement_snapshots
  ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS settled_via_file_id BIGINT;

ALTER TABLE settlement_snapshots
  DROP CONSTRAINT IF EXISTS settlement_snapshots_settled_via_file_id_fkey;
ALTER TABLE settlement_snapshots
  ADD CONSTRAINT settlement_snapshots_settled_via_file_id_fkey
  FOREIGN KEY (settled_via_file_id) REFERENCES settlement_files(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_settlement_snapshots_settled_via_file
  ON settlement_snapshots (settled_via_file_id);

CREATE OR REPLACE FUNCTION prevent_settlement_snapshot_payload_mutation()
RETURNS TRIGGER AS $$
BEGIN
  -- Allow only the settle-marker fields to change. Block everything else.
  IF (
    NEW.id IS DISTINCT FROM OLD.id
    OR NEW.billing_run_id IS DISTINCT FROM OLD.billing_run_id
    OR NEW.channel_partner_id IS DISTINCT FROM OLD.channel_partner_id
    OR NEW.snapshot_payload_jsonb IS DISTINCT FROM OLD.snapshot_payload_jsonb
    OR NEW.computed_comp_cents IS DISTINCT FROM OLD.computed_comp_cents
    OR NEW.subsidy_cap_remaining_at_snapshot IS DISTINCT FROM OLD.subsidy_cap_remaining_at_snapshot
    OR NEW.created_at IS DISTINCT FROM OLD.created_at
  ) THEN
    RAISE EXCEPTION 'settlement_snapshots row % does not allow mutation of computed fields', OLD.id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_settlement_snapshots_append_only ON settlement_snapshots;
DROP TRIGGER IF EXISTS trg_settlement_snapshots_settle_only ON settlement_snapshots;
CREATE TRIGGER trg_settlement_snapshots_settle_only
  BEFORE UPDATE ON settlement_snapshots
  FOR EACH ROW EXECUTE PROCEDURE prevent_settlement_snapshot_payload_mutation();

-- DELETE is still blocked: separate trigger using the original append-only function.
DROP TRIGGER IF EXISTS trg_settlement_snapshots_no_delete ON settlement_snapshots;
CREATE TRIGGER trg_settlement_snapshots_no_delete
  BEFORE DELETE ON settlement_snapshots
  FOR EACH ROW EXECUTE PROCEDURE prevent_monetization_append_only_mutation();

COMMENT ON TABLE settlement_files IS
  'Monthly partner settlement files. Generated on day 5 from settlement_snapshots; Stripe Connect transfer on day 10. Negative-net months carry forward via carryover_forward_cents (always <= 0).';

COMMENT ON COLUMN settlement_snapshots.settled_at IS
  'Timestamp when this settlement snapshot was consumed by a settlement file transfer or zero-net closeout.';
COMMENT ON COLUMN settlement_snapshots.settled_via_file_id IS
  'Settlement file that consumed this snapshot for audit lineage.';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_settlement_snapshots_no_delete ON settlement_snapshots;
-- DROP TRIGGER IF EXISTS trg_settlement_snapshots_settle_only ON settlement_snapshots;
-- DROP FUNCTION IF EXISTS prevent_settlement_snapshot_payload_mutation();
-- DROP TRIGGER IF EXISTS trg_settlement_files_updated_at ON settlement_files;
-- DROP TABLE IF EXISTS settlement_files;
