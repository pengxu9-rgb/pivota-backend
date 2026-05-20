-- 119_invoice_finalizing_status.sql
-- Promote T7 invoice_generation_service.py runtime schema guards into canonical migration.
-- Fixes blocker: invoices.status='finalizing' (set mid-Stripe-finalize-call) violated
-- the 113/118 CHECK constraint that did not include 'finalizing'.
-- Also formalizes the invoice_disputes 'applied' status used by T7 handle_dispute,
-- and the additive columns T7 currently runtime-ALTERs at module load.

ALTER TABLE IF EXISTS invoices
  ADD COLUMN IF NOT EXISTS billing_run_id BIGINT;
ALTER TABLE IF EXISTS invoices
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;

ALTER TABLE IF EXISTS billing_run_items
  ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ;

ALTER TABLE IF EXISTS invoice_disputes
  ADD COLUMN IF NOT EXISTS disputed_line_items_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE IF EXISTS invoices
  DROP CONSTRAINT IF EXISTS ck_invoices_status;
ALTER TABLE IF EXISTS invoices
  ADD CONSTRAINT ck_invoices_status CHECK (
    status IN ('draft', 'finalizing', 'finalized', 'paid', 'failed', 'payment_failed', 'void', 'uncollectible')
  );

ALTER TABLE IF EXISTS invoice_disputes
  DROP CONSTRAINT IF EXISTS ck_invoice_disputes_status;
ALTER TABLE IF EXISTS invoice_disputes
  ADD CONSTRAINT ck_invoice_disputes_status CHECK (
    status IN ('open', 'under_review', 'applied', 'resolved', 'rejected', 'cancelled')
  );

ALTER TABLE IF EXISTS invoice_disputes
  DROP CONSTRAINT IF EXISTS ck_invoice_disputes_resolved_status;
ALTER TABLE IF EXISTS invoice_disputes
  ADD CONSTRAINT ck_invoice_disputes_resolved_status CHECK (
    resolved_at IS NULL OR status IN ('applied', 'resolved', 'rejected', 'cancelled')
  );

CREATE INDEX IF NOT EXISTS idx_invoices_billing_run_id
  ON invoices(billing_run_id);
CREATE INDEX IF NOT EXISTS idx_invoices_stripe_customer_id
  ON invoices(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_billing_run_items_voided_at
  ON billing_run_items(voided_at)
  WHERE voided_at IS NOT NULL;

COMMENT ON CONSTRAINT ck_invoices_status ON invoices IS 'v1.3 invoice lifecycle: finalizing is intermediate during Stripe finalize_invoice API call';
COMMENT ON CONSTRAINT ck_invoice_disputes_status ON invoice_disputes IS 'v1.3 dispute lifecycle: applied = adjustment posted to draft invoice as InvoiceItem delete+replace';
COMMENT ON COLUMN invoices.billing_run_id IS 'FK to billing_runs.id for the cycle that generated this invoice';
COMMENT ON COLUMN invoices.stripe_customer_id IS 'Denormalized Stripe customer ID for invoice lookups without joining merchants';
COMMENT ON COLUMN billing_run_items.voided_at IS 'Set when an InvoiceItem was voided via Stripe invoice_items.delete during dispute handling';
COMMENT ON COLUMN invoice_disputes.disputed_line_items_jsonb IS 'JSON array describing the billing_run_items to void and their adjusted amounts';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP INDEX IF EXISTS idx_billing_run_items_voided_at;
-- DROP INDEX IF EXISTS idx_invoices_stripe_customer_id;
-- DROP INDEX IF EXISTS idx_invoices_billing_run_id;
-- ALTER TABLE IF EXISTS invoice_disputes DROP CONSTRAINT IF EXISTS ck_invoice_disputes_resolved_status;
-- ALTER TABLE IF EXISTS invoice_disputes DROP CONSTRAINT IF EXISTS ck_invoice_disputes_status;
-- ALTER TABLE IF EXISTS invoices DROP CONSTRAINT IF EXISTS ck_invoices_status;
-- ALTER TABLE IF EXISTS invoice_disputes DROP COLUMN IF EXISTS disputed_line_items_jsonb;
-- ALTER TABLE IF EXISTS billing_run_items DROP COLUMN IF EXISTS voided_at;
-- ALTER TABLE IF EXISTS invoices DROP COLUMN IF EXISTS stripe_customer_id;
-- ALTER TABLE IF EXISTS invoices DROP COLUMN IF EXISTS billing_run_id;
