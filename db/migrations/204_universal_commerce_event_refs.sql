-- 204: universal commerce event stitching references.
--
-- The canonical interaction ledger originally carried checkout/order/refund ids,
-- but a platform-neutral funnel also needs the store, cart, payment attempt, and
-- anonymous visitor references that bridge browser, webhook, and server events.
-- These are nullable and additive so existing producers continue unchanged.

ALTER TABLE IF EXISTS commerce_interactions
  ADD COLUMN IF NOT EXISTS store_id VARCHAR(128),
  ADD COLUMN IF NOT EXISTS cart_id VARCHAR(128),
  ADD COLUMN IF NOT EXISTS payment_id VARCHAR(128),
  ADD COLUMN IF NOT EXISTS visitor_id VARCHAR(128);

ALTER TABLE IF EXISTS commerce_interactions
  ALTER COLUMN checkout_id TYPE VARCHAR(128),
  ALTER COLUMN order_id TYPE VARCHAR(128),
  ALTER COLUMN refund_id TYPE VARCHAR(128),
  ALTER COLUMN return_id TYPE VARCHAR(128);

ALTER TABLE IF EXISTS commerce_interaction_events
  ADD COLUMN IF NOT EXISTS store_id VARCHAR(128),
  ADD COLUMN IF NOT EXISTS cart_id VARCHAR(128),
  ADD COLUMN IF NOT EXISTS payment_id VARCHAR(128),
  ADD COLUMN IF NOT EXISTS visitor_id VARCHAR(128);

CREATE INDEX IF NOT EXISTS idx_commerce_interactions_store
  ON commerce_interactions(merchant_id, platform, store_id);
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_store_cart
  ON commerce_interactions(merchant_id, store_id, cart_id)
  WHERE cart_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_store_payment
  ON commerce_interactions(merchant_id, store_id, payment_id)
  WHERE payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_store_session
  ON commerce_interactions(merchant_id, store_id, session_id)
  WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_store
  ON commerce_interaction_events(merchant_id, platform, store_id);
CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_cart
  ON commerce_interaction_events(merchant_id, cart_id)
  WHERE cart_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_payment
  ON commerce_interaction_events(merchant_id, payment_id)
  WHERE payment_id IS NOT NULL;
