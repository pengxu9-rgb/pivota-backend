-- 106_credit_ledger.sql
-- Append-only credit balance audit log.

CREATE TABLE IF NOT EXISTS credit_ledger (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  operation_type TEXT NOT NULL,
  operation_id TEXT,
  credits_delta BIGINT NOT NULL,
  balance_after BIGINT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reservation_id BIGINT REFERENCES credit_reservations(id) ON DELETE SET NULL,
  source_payment_intent_id TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_credit_ledger_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_credit_ledger_operation_type_nonempty CHECK (operation_type <> ''),
  CONSTRAINT ck_credit_ledger_balance_after_nonnegative CHECK (balance_after >= 0)
);

CREATE INDEX IF NOT EXISTS idx_credit_ledger_merchant_id
  ON credit_ledger(merchant_id);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_operation
  ON credit_ledger(operation_type, operation_id);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_reservation_id
  ON credit_ledger(reservation_id);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_source_payment_intent_id
  ON credit_ledger(source_payment_intent_id);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_occurred_at
  ON credit_ledger(occurred_at DESC);

CREATE OR REPLACE FUNCTION prevent_monetization_append_only_mutation()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'monetization append-only table % does not allow UPDATE or DELETE', TG_TABLE_NAME;
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_credit_ledger_append_only ON credit_ledger;
CREATE TRIGGER trg_credit_ledger_append_only
  BEFORE UPDATE OR DELETE ON credit_ledger
  FOR EACH ROW EXECUTE PROCEDURE prevent_monetization_append_only_mutation();

COMMENT ON TABLE credit_ledger IS 'Monetization v1.3: append-only audit log of every credit-balance change; UPDATE and DELETE are blocked by trigger';
COMMENT ON COLUMN credit_ledger.credits_delta IS 'Signed credit balance delta';
COMMENT ON COLUMN credit_ledger.balance_after IS 'Credit balance after applying this ledger event';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_credit_ledger_append_only ON credit_ledger;
-- DROP TABLE IF EXISTS credit_ledger;
