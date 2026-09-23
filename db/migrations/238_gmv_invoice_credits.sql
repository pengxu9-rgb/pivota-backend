-- Credits for GMV take already invoiced on a day a later refund reduced.
--
-- The same table is declared in db/gmv_invoice_credits.py, and main.py's metadata.create_all creates
-- it at startup. This file is the reviewed DDL for environments that apply numbered migrations.
-- Keep the two in step.
--
-- One row per credit against one invoiced line (billing_run_items, source_type 'gmv_rollup'):
-- computed automatically as 'pending', approved by an admin, then issued to Stripe as a credit note
-- (against the amount due on an open invoice, or to the customer balance on a paid one; never a
-- cash refund). services/gmv_invoice_credits.py.

CREATE TABLE IF NOT EXISTS gmv_invoice_credits (
  id BIGSERIAL PRIMARY KEY,
  billing_run_item_id BIGINT NOT NULL,
  billing_run_id BIGINT NOT NULL,
  rollup_id BIGINT NOT NULL,
  merchant_id VARCHAR(100) NOT NULL,
  channel_partner_id BIGINT,
  billed_day DATE NOT NULL,
  stripe_invoice_id TEXT NOT NULL,
  currency VARCHAR(8) NOT NULL DEFAULT 'USD',
  amount_cents BIGINT NOT NULL,
  billed_cents BIGINT NOT NULL,
  correct_take_cents BIGINT NOT NULL,
  committed_before_cents BIGINT NOT NULL,
  take_rate_bp INTEGER NOT NULL,
  gross_cents BIGINT NOT NULL,
  refund_cents BIGINT NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  status_reason TEXT,
  approved_by VARCHAR(255),
  approved_at TIMESTAMPTZ,
  cancelled_by VARCHAR(255),
  cancelled_at TIMESTAMPTZ,
  issue_attempts INTEGER NOT NULL DEFAULT 0,
  stripe_credit_note_id TEXT,
  stripe_credit_kind VARCHAR(32),
  issued_at TIMESTAMPTZ,
  last_error TEXT,
  partner_status VARCHAR(32),
  partner_snapshot_id BIGINT,
  partner_clawback_cents BIGINT,
  partner_decided_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_gmv_invoice_credits_amount_positive CHECK (amount_cents > 0),
  CONSTRAINT ck_gmv_invoice_credits_status CHECK (
    status IN ('pending', 'approved', 'issuing', 'issued', 'failed', 'cancelled')
  ),
  CONSTRAINT ck_gmv_invoice_credits_partner_status CHECK (
    partner_status IS NULL OR partner_status IN ('not_applicable', 'netted', 'clawed_back', 'v2_unhandled')
  ),
  CONSTRAINT ck_gmv_invoice_credits_issued_has_note CHECK (
    status <> 'issued' OR stripe_credit_note_id IS NOT NULL
  )
);

CREATE INDEX IF NOT EXISTS idx_gmv_invoice_credits_item ON gmv_invoice_credits (billing_run_item_id);
CREATE INDEX IF NOT EXISTS idx_gmv_invoice_credits_status ON gmv_invoice_credits (status, created_at);
CREATE INDEX IF NOT EXISTS idx_gmv_invoice_credits_rollup ON gmv_invoice_credits (rollup_id);
-- At most ONE open (pending) credit per line: a recompute adjusts it instead of stacking another.
CREATE UNIQUE INDEX IF NOT EXISTS uq_gmv_invoice_credits_one_pending_per_item
  ON gmv_invoice_credits (billing_run_item_id) WHERE status = 'pending';
