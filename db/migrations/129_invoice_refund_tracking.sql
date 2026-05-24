-- 129_invoice_refund_tracking.sql
-- Track refunded invoice cash locally so activation can use net cash received.

ALTER TABLE invoices
  ADD COLUMN IF NOT EXISTS refunded_cents BIGINT NOT NULL DEFAULT 0;

ALTER TABLE invoices
  DROP CONSTRAINT IF EXISTS ck_invoices_refunded_cents_nonneg;
ALTER TABLE invoices
  ADD CONSTRAINT ck_invoices_refunded_cents_nonneg CHECK (refunded_cents >= 0);

ALTER TABLE invoices
  DROP CONSTRAINT IF EXISTS ck_invoices_refunded_lte_total;
ALTER TABLE invoices
  ADD CONSTRAINT ck_invoices_refunded_lte_total CHECK (refunded_cents <= total_cents);

COMMENT ON COLUMN invoices.refunded_cents IS
  'Local refunded amount in cents used for net-cash activation eligibility. Populated manually or by future refund webhooks.';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE invoices DROP CONSTRAINT IF EXISTS ck_invoices_refunded_lte_total;
-- ALTER TABLE invoices DROP CONSTRAINT IF EXISTS ck_invoices_refunded_cents_nonneg;
-- ALTER TABLE invoices DROP COLUMN IF EXISTS refunded_cents;
