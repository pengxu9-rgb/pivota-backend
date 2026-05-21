-- 113_billing_core.sql
-- Monthly billing run, invoice mirror, disputes, and Stripe invoice item audit mapping.

CREATE TABLE IF NOT EXISTS billing_runs (
  id BIGSERIAL PRIMARY KEY,
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_billing_runs_idempotency_key UNIQUE (idempotency_key),
  CONSTRAINT ck_billing_runs_idempotency_key_nonempty CHECK (idempotency_key <> ''),
  CONSTRAINT ck_billing_runs_status CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
  CONSTRAINT ck_billing_runs_period_order CHECK (period_end > period_start),
  CONSTRAINT ck_billing_runs_completed_status CHECK (
    completed_at IS NULL OR status IN ('completed', 'failed', 'cancelled')
  )
);

CREATE INDEX IF NOT EXISTS idx_billing_runs_period
  ON billing_runs(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_billing_runs_idempotency_key
  ON billing_runs(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_billing_runs_status
  ON billing_runs(status);
CREATE INDEX IF NOT EXISTS idx_billing_runs_started_at
  ON billing_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS invoices (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  billing_period_start TIMESTAMPTZ NOT NULL,
  billing_period_end TIMESTAMPTZ NOT NULL,
  stripe_invoice_id TEXT,
  total_cents BIGINT NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'draft',
  due_date TIMESTAMPTZ,
  finalized_at TIMESTAMPTZ,
  disputed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_invoices_stripe_invoice_id UNIQUE (stripe_invoice_id),
  CONSTRAINT ck_invoices_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_invoices_total_cents_nonnegative CHECK (total_cents >= 0),
  CONSTRAINT ck_invoices_status CHECK (
    status IN ('draft', 'finalized', 'paid', 'failed', 'void', 'uncollectible')
  ),
  CONSTRAINT ck_invoices_billing_period_order CHECK (billing_period_end > billing_period_start)
);

CREATE INDEX IF NOT EXISTS idx_invoices_merchant_id
  ON invoices(merchant_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status
  ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_billing_period
  ON invoices(billing_period_start, billing_period_end);
CREATE INDEX IF NOT EXISTS idx_invoices_due_date
  ON invoices(due_date);

CREATE TABLE IF NOT EXISTS invoice_disputes (
  id BIGSERIAL PRIMARY KEY,
  invoice_id BIGINT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  merchant_id VARCHAR(50) NOT NULL,
  disputed_edge_ids_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMPTZ,
  resolution_notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_invoice_disputes_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_invoice_disputes_reason_nonempty CHECK (reason <> ''),
  CONSTRAINT ck_invoice_disputes_status CHECK (
    status IN ('open', 'under_review', 'resolved', 'rejected', 'cancelled')
  ),
  CONSTRAINT ck_invoice_disputes_edge_ids_array CHECK (
    jsonb_typeof(disputed_edge_ids_jsonb) = 'array'
  ),
  CONSTRAINT ck_invoice_disputes_resolved_status CHECK (
    resolved_at IS NULL OR status IN ('resolved', 'rejected', 'cancelled')
  )
);

CREATE INDEX IF NOT EXISTS idx_invoice_disputes_invoice_id
  ON invoice_disputes(invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoice_disputes_merchant_id
  ON invoice_disputes(merchant_id);
CREATE INDEX IF NOT EXISTS idx_invoice_disputes_status
  ON invoice_disputes(status);
CREATE INDEX IF NOT EXISTS idx_invoice_disputes_opened_at
  ON invoice_disputes(opened_at DESC);

CREATE TABLE IF NOT EXISTS billing_run_items (
  id BIGSERIAL PRIMARY KEY,
  billing_run_id BIGINT NOT NULL REFERENCES billing_runs(id) ON DELETE CASCADE,
  merchant_id VARCHAR(50) NOT NULL,
  source_type TEXT NOT NULL,
  source_id BIGINT NOT NULL,
  stripe_invoice_item_id TEXT NOT NULL,
  stripe_invoice_id TEXT NOT NULL,
  amount_cents BIGINT NOT NULL,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finalized_at TIMESTAMPTZ,
  CONSTRAINT uq_billing_run_items_source_invoice_item UNIQUE (
    billing_run_id,
    source_type,
    source_id,
    stripe_invoice_item_id
  ),
  CONSTRAINT ck_billing_run_items_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_billing_run_items_source_type CHECK (
    source_type IN ('gmv_rollup', 'dispute_adj', 'credit_note', 'topup_refund')
  ),
  CONSTRAINT ck_billing_run_items_stripe_invoice_item_id_nonempty CHECK (stripe_invoice_item_id <> ''),
  CONSTRAINT ck_billing_run_items_stripe_invoice_id_nonempty CHECK (stripe_invoice_id <> '')
);

CREATE INDEX IF NOT EXISTS idx_billing_run_items_billing_run_id
  ON billing_run_items(billing_run_id);
CREATE INDEX IF NOT EXISTS idx_billing_run_items_merchant_id
  ON billing_run_items(merchant_id);
CREATE INDEX IF NOT EXISTS idx_billing_run_items_source
  ON billing_run_items(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_billing_run_items_stripe_invoice_item_id
  ON billing_run_items(stripe_invoice_item_id);
CREATE INDEX IF NOT EXISTS idx_billing_run_items_stripe_invoice_id
  ON billing_run_items(stripe_invoice_id);
CREATE INDEX IF NOT EXISTS idx_billing_run_items_finalized_at
  ON billing_run_items(finalized_at);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_billing_runs_updated_at ON billing_runs;
CREATE TRIGGER trg_billing_runs_updated_at
  BEFORE UPDATE ON billing_runs
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

DROP TRIGGER IF EXISTS trg_invoices_updated_at ON invoices;
CREATE TRIGGER trg_invoices_updated_at
  BEFORE UPDATE ON invoices
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

DROP TRIGGER IF EXISTS trg_invoice_disputes_updated_at ON invoice_disputes;
CREATE TRIGGER trg_invoice_disputes_updated_at
  BEFORE UPDATE ON invoice_disputes
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE billing_runs IS 'Monetization v1.3: one row per monthly billing cycle execution';
COMMENT ON COLUMN billing_runs.idempotency_key IS 'Unique period key so duplicate cron attempts return the existing billing run';
COMMENT ON TABLE invoices IS 'Monetization v1.3: local mirror of Stripe GMV-take invoices';
COMMENT ON TABLE invoice_disputes IS 'Monetization v1.3: merchant-filed disputes against invoice line items';
COMMENT ON TABLE billing_run_items IS 'Monetization v1.3: audit map from source rollup or adjustment to Stripe invoice item';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_invoice_disputes_updated_at ON invoice_disputes;
-- DROP TRIGGER IF EXISTS trg_invoices_updated_at ON invoices;
-- DROP TRIGGER IF EXISTS trg_billing_runs_updated_at ON billing_runs;
-- DROP TABLE IF EXISTS billing_run_items;
-- DROP TABLE IF EXISTS invoice_disputes;
-- DROP TABLE IF EXISTS invoices;
-- DROP TABLE IF EXISTS billing_runs;
