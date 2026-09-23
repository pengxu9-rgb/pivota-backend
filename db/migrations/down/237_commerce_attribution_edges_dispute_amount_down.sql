-- Reverse of 237_commerce_attribution_edges_dispute_amount.sql.
--
-- refund_amount_cents keeps the chargeback money it already holds. Only the split
-- is lost. Deploy code that still reads dispute_amount_cents after this and every
-- refund write to the edge fails (it is logged and swallowed, so the edge silently
-- stops moving).

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_dispute_amount_cents;
ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP COLUMN IF EXISTS dispute_amount_cents;
