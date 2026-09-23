-- 237_commerce_attribution_edges_dispute_amount.sql
-- Keep chargebacks as their own total on the attribution edge.
--
-- refund_amount_cents is now two things added together:
--   * PSP refunds. These follow the order's reconciled orders.total_refunded as a
--     ceiling (commerce_attribution_service.apply_refund_total_to_attribution_edge),
--     because Stripe reports one refund several times under different ids
--     (ch_ cumulative, re_ per refund, and our own REF_ for merchant-initiated
--     refunds). Adding each id's amount counted the same money two or three times.
--   * Chargebacks. These are NOT in orders.total_refunded, so a ceiling taken from
--     the order would swallow them. They stay additive per dispute id and are
--     counted here as well, so the ceiling can be applied to
--     refund_amount_cents - dispute_amount_cents alone.
--
-- Existing rows start at 0. A dispute recorded on an edge BEFORE this column
-- existed is therefore still inside the refund part, and a later refund ceiling
-- can absorb up to that much. The ceiling never lowers a value, so existing
-- over-counted rows are left as they are; correcting them is a separate recompute.

ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS dispute_amount_cents BIGINT NOT NULL DEFAULT 0;

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_dispute_amount_cents;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_dispute_amount_cents
  CHECK (dispute_amount_cents >= 0);

COMMENT ON COLUMN commerce_attribution_edges.dispute_amount_cents IS
  'Chargeback part of refund_amount_cents, additive per dispute id; the rest follows orders.total_refunded';
