-- 118_invoice_payment_failed_status.sql
-- Align local invoice mirror with Stripe Billing webhook statuses.

ALTER TABLE IF EXISTS invoices
  ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ;

ALTER TABLE IF EXISTS invoices
  DROP CONSTRAINT IF EXISTS ck_invoices_status;
ALTER TABLE IF EXISTS invoices
  ADD CONSTRAINT ck_invoices_status CHECK (
    status IN ('draft', 'finalized', 'paid', 'failed', 'payment_failed', 'void', 'uncollectible')
  );

COMMENT ON COLUMN invoices.paid_at IS 'Timestamp when Stripe reported invoice.paid';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS invoices DROP CONSTRAINT IF EXISTS ck_invoices_status;
-- ALTER TABLE IF EXISTS invoices ADD CONSTRAINT ck_invoices_status CHECK (
--   status IN ('draft', 'finalized', 'paid', 'failed', 'void', 'uncollectible')
-- );
-- ALTER TABLE IF EXISTS invoices DROP COLUMN IF EXISTS paid_at;
