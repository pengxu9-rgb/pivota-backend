-- 205: scope external commerce references to their merchant/store namespace.
--
-- Store platforms commonly allocate small local order IDs (for example, two
-- WooCommerce stores can both have order 123). The original global unique
-- indexes made those legitimate rows collide and could stitch one store's
-- lifecycle into another store's interaction.

DROP INDEX IF EXISTS idx_commerce_interactions_click_id_unique;
DROP INDEX IF EXISTS idx_commerce_interactions_quote_id_unique;
DROP INDEX IF EXISTS idx_commerce_interactions_checkout_id_unique;
DROP INDEX IF EXISTS idx_commerce_interactions_order_id_unique;
DROP INDEX IF EXISTS idx_commerce_interactions_refund_id_unique;
DROP INDEX IF EXISTS idx_commerce_interactions_return_id_unique;

CREATE UNIQUE INDEX idx_commerce_interactions_click_id_unique
  ON commerce_interactions(merchant_id, COALESCE(store_id, ''), click_id)
  WHERE click_id IS NOT NULL;
CREATE UNIQUE INDEX idx_commerce_interactions_quote_id_unique
  ON commerce_interactions(merchant_id, COALESCE(store_id, ''), quote_id)
  WHERE quote_id IS NOT NULL;
CREATE UNIQUE INDEX idx_commerce_interactions_checkout_id_unique
  ON commerce_interactions(merchant_id, COALESCE(store_id, ''), checkout_id)
  WHERE checkout_id IS NOT NULL;
CREATE UNIQUE INDEX idx_commerce_interactions_order_id_unique
  ON commerce_interactions(merchant_id, COALESCE(store_id, ''), order_id)
  WHERE order_id IS NOT NULL;
CREATE UNIQUE INDEX idx_commerce_interactions_refund_id_unique
  ON commerce_interactions(merchant_id, COALESCE(store_id, ''), refund_id)
  WHERE refund_id IS NOT NULL;
CREATE UNIQUE INDEX idx_commerce_interactions_return_id_unique
  ON commerce_interactions(merchant_id, COALESCE(store_id, ''), return_id)
  WHERE return_id IS NOT NULL;
