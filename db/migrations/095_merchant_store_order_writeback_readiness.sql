-- Store-scoped platform order writeback readiness.
--
-- Runtime policy:
--   disabled/paused/failed/unknown -> fail closed
--   canary -> only the matching canary order may write back
--   enabled -> active store may write back normally

ALTER TABLE merchant_stores
  ADD COLUMN IF NOT EXISTS order_writeback_status TEXT NOT NULL DEFAULT 'disabled',
  ADD COLUMN IF NOT EXISTS order_writeback_enabled_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS order_writeback_canary_order_id TEXT NULL,
  ADD COLUMN IF NOT EXISTS order_writeback_last_canary_order_id TEXT NULL,
  ADD COLUMN IF NOT EXISTS order_writeback_last_verified_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS order_writeback_last_error TEXT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'merchant_stores_order_writeback_status_chk'
  ) THEN
    ALTER TABLE merchant_stores
      ADD CONSTRAINT merchant_stores_order_writeback_status_chk
      CHECK (
        order_writeback_status IN (
          'disabled',
          'canary',
          'enabled',
          'paused',
          'failed'
        )
      ) NOT VALID;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_merchant_stores_order_writeback_status
  ON merchant_stores (platform, order_writeback_status, status);

CREATE INDEX IF NOT EXISTS idx_merchant_stores_order_writeback_canary
  ON merchant_stores (order_writeback_canary_order_id)
  WHERE order_writeback_canary_order_id IS NOT NULL;
