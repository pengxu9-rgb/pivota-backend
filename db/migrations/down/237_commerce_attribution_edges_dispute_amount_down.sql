-- Reverse of 237_commerce_attribution_edges_dispute_amount.sql.
--
-- refund_amount_cents keeps the chargeback money it already holds. Only the split
-- is lost.
--
-- Do NOT run this while code that names dispute_amount_cents is deployed. Every
-- refund and dispute write to the edge then fails. The webhook writers log it and
-- the edge stops moving. RefundService.create_refund makes its edge write inside
-- the refund's own transaction: it is wrapped in a savepoint, so the refund record
-- survives, but without that savepoint the failed statement would abort the
-- transaction and LOSE the refund record while the PSP had already refunded.
-- schema_guard.py also re-adds the column at the next boot, so dropping it here
-- without also removing that heal does not stick.

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_dispute_amount_cents;
ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP COLUMN IF EXISTS dispute_amount_cents;
