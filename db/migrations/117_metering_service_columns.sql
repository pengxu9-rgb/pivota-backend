-- 117_metering_service_columns.sql
-- Non-destructive columns used by services/metering_service.py.

ALTER TABLE IF EXISTS credit_reservations
  ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE IF EXISTS credit_ledger
  ADD COLUMN IF NOT EXISTS source_type TEXT;

ALTER TABLE IF EXISTS credit_ledger
  DROP CONSTRAINT IF EXISTS ck_credit_ledger_source_type_nonempty;
ALTER TABLE IF EXISTS credit_ledger
  ADD CONSTRAINT ck_credit_ledger_source_type_nonempty
  CHECK (source_type IS NULL OR source_type <> '');

CREATE INDEX IF NOT EXISTS idx_credit_ledger_source_type
  ON credit_ledger(source_type);

COMMENT ON COLUMN credit_reservations.metadata IS 'Optional metering caller metadata captured with the reservation';
COMMENT ON COLUMN credit_ledger.source_type IS 'Internal source category for the credit ledger event';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS credit_ledger DROP CONSTRAINT IF EXISTS ck_credit_ledger_source_type_nonempty;
-- ALTER TABLE IF EXISTS credit_ledger DROP COLUMN IF EXISTS source_type;
-- ALTER TABLE IF EXISTS credit_reservations DROP COLUMN IF EXISTS metadata;
