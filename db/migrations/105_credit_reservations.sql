-- 105_credit_reservations.sql
-- Pre-commit credit holds for reserve, commit, release, and expire lifecycle.

CREATE TABLE IF NOT EXISTS credit_reservations (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  operation_type TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  credits_held BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'reserved',
  reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finalized_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_credit_reservations_operation UNIQUE (merchant_id, operation_type, operation_id),
  CONSTRAINT ck_credit_reservations_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_credit_reservations_operation_type_nonempty CHECK (operation_type <> ''),
  CONSTRAINT ck_credit_reservations_operation_id_nonempty CHECK (operation_id <> ''),
  CONSTRAINT ck_credit_reservations_credits_held_positive CHECK (credits_held > 0),
  CONSTRAINT ck_credit_reservations_status CHECK (
    status IN ('reserved', 'committed', 'released', 'expired')
  ),
  CONSTRAINT ck_credit_reservations_expiry_order CHECK (expires_at >= reserved_at),
  CONSTRAINT ck_credit_reservations_finalized_status CHECK (
    (status = 'reserved' AND finalized_at IS NULL)
    OR
    (status <> 'reserved' AND finalized_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_credit_reservations_merchant_id
  ON credit_reservations(merchant_id);
CREATE INDEX IF NOT EXISTS idx_credit_reservations_status
  ON credit_reservations(status);
CREATE INDEX IF NOT EXISTS idx_credit_reservations_expires_at
  ON credit_reservations(expires_at);
CREATE INDEX IF NOT EXISTS idx_credit_reservations_operation
  ON credit_reservations(operation_type, operation_id);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_credit_reservations_updated_at ON credit_reservations;
CREATE TRIGGER trg_credit_reservations_updated_at
  BEFORE UPDATE ON credit_reservations
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE credit_reservations IS 'Monetization v1.3: credit holds before operation commit or release';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_credit_reservations_updated_at ON credit_reservations;
-- DROP TABLE IF EXISTS credit_reservations;
