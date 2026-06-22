-- Store-scoped content writeback readiness (the metafield rung).
--
-- Pivota writes AI-ready PDP copy to an APP-OWNED Shopify metafield
-- ($app:pivota/ai_pdp), never body_html, and only when a store is explicitly
-- opted in here. Mirrors the order-writeback state machine (migration 095).
--
-- Runtime policy (fail closed):
--   disabled/paused/failed/unknown -> no write
--   canary                         -> only the matching canary product may write
--   enabled                        -> active store may write normally
-- Plus a global kill switch env DISABLE_CONTENT_WRITEBACK and, at write time,
-- the merchant's connected (BYO / custom app) Admin token must carry
-- write_products, or the metafieldsSet ACCESS_DENIED is surfaced as
-- needs_write_products (enable it on your app) — no partial write.

ALTER TABLE merchant_stores
  ADD COLUMN IF NOT EXISTS content_writeback_status TEXT NOT NULL DEFAULT 'disabled',
  ADD COLUMN IF NOT EXISTS content_writeback_enabled_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS content_writeback_canary_product_id TEXT NULL,
  ADD COLUMN IF NOT EXISTS content_writeback_last_canary_product_id TEXT NULL,
  ADD COLUMN IF NOT EXISTS content_writeback_last_written_at TIMESTAMPTZ NULL,
  ADD COLUMN IF NOT EXISTS content_writeback_last_error TEXT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'merchant_stores_content_writeback_status_chk'
  ) THEN
    ALTER TABLE merchant_stores
      ADD CONSTRAINT merchant_stores_content_writeback_status_chk
      CHECK (
        content_writeback_status IN (
          'disabled',
          'canary',
          'enabled',
          'paused',
          'failed'
        )
      ) NOT VALID;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_merchant_stores_content_writeback_status
  ON merchant_stores (platform, content_writeback_status, status);

CREATE INDEX IF NOT EXISTS idx_merchant_stores_content_writeback_canary
  ON merchant_stores (content_writeback_canary_product_id)
  WHERE content_writeback_canary_product_id IS NOT NULL;
