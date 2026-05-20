-- 115_partner_balance_ledger.sql
-- Append-only partner balance event ledger.

CREATE TABLE IF NOT EXISTS partner_balance_ledger (
  id BIGSERIAL PRIMARY KEY,
  channel_partner_id BIGINT NOT NULL REFERENCES channel_partners(id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL,
  amount_cents BIGINT NOT NULL,
  balance_after BIGINT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source_snapshot_id BIGINT REFERENCES settlement_snapshots(id) ON DELETE SET NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_partner_balance_ledger_event_type CHECK (
    event_type IN ('settlement_added', 'clawback', 'payout', 'adjustment')
  )
);

CREATE INDEX IF NOT EXISTS idx_partner_balance_ledger_channel_partner_id
  ON partner_balance_ledger(channel_partner_id);
CREATE INDEX IF NOT EXISTS idx_partner_balance_ledger_event_type
  ON partner_balance_ledger(event_type);
CREATE INDEX IF NOT EXISTS idx_partner_balance_ledger_source_snapshot_id
  ON partner_balance_ledger(source_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_partner_balance_ledger_occurred_at
  ON partner_balance_ledger(occurred_at DESC);

CREATE OR REPLACE FUNCTION prevent_monetization_append_only_mutation()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'monetization append-only table % does not allow UPDATE or DELETE', TG_TABLE_NAME;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_partner_balance_ledger_append_only ON partner_balance_ledger;
CREATE TRIGGER trg_partner_balance_ledger_append_only
  BEFORE UPDATE OR DELETE ON partner_balance_ledger
  FOR EACH ROW EXECUTE PROCEDURE prevent_monetization_append_only_mutation();

COMMENT ON TABLE partner_balance_ledger IS 'Monetization v1.3: append-only audit log of partner balance credits and debits; UPDATE and DELETE are blocked by trigger';
COMMENT ON COLUMN partner_balance_ledger.balance_after IS 'Partner running balance after applying this ledger event';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_partner_balance_ledger_append_only ON partner_balance_ledger;
-- DROP TABLE IF EXISTS partner_balance_ledger;
