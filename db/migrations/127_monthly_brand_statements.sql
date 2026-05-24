-- 127_monthly_brand_statements.sql
-- Canonical per-brand monthly monetization statement and credit-overage source typing.

CREATE TABLE IF NOT EXISTS monthly_brand_statements (
  id BIGSERIAL PRIMARY KEY
);

ALTER TABLE IF EXISTS monthly_brand_statements
  ADD COLUMN IF NOT EXISTS merchant_id VARCHAR(50) NOT NULL,
  ADD COLUMN IF NOT EXISTS calendar_month DATE NOT NULL,
  ADD COLUMN IF NOT EXISTS subscription_plan_id BIGINT,
  ADD COLUMN IF NOT EXISTS tier_name TEXT,
  ADD COLUMN IF NOT EXISTS subscription_revenue_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS credits_consumed BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS bundled_credits_consumed BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS overage_credits BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS overage_revenue_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS gmv_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS gmv_personal_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS gmv_third_party_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS pivota_gmv_take_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_revenue_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS total_cogs_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS pivota_gross_margin_usd_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open',
  ADD COLUMN IF NOT EXISTS frozen_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS invoiced_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS overage_invoice_id BIGINT,
  ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS monthly_brand_statements_subscription_plan_id_fkey;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT monthly_brand_statements_subscription_plan_id_fkey
  FOREIGN KEY (subscription_plan_id) REFERENCES subscription_plans(id) ON DELETE RESTRICT;

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS monthly_brand_statements_overage_invoice_id_fkey;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT monthly_brand_statements_overage_invoice_id_fkey
  FOREIGN KEY (overage_invoice_id) REFERENCES invoices(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS uq_monthly_brand_statements_merchant_month;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT uq_monthly_brand_statements_merchant_month
  UNIQUE (merchant_id, calendar_month);

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_status;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_status
  CHECK (status IN ('open', 'frozen', 'invoiced'));

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_subscription_revenue_nonnegative;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_subscription_revenue_nonnegative
  CHECK (subscription_revenue_usd_cents >= 0);

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_credit_counts_nonnegative;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_credit_counts_nonnegative
  CHECK (
    credits_consumed >= 0
    AND bundled_credits_consumed >= 0
    AND overage_credits >= 0
  );

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_credit_split;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_credit_split
  CHECK (bundled_credits_consumed + overage_credits = credits_consumed);

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_overage_revenue_nonnegative;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_overage_revenue_nonnegative
  CHECK (overage_revenue_usd_cents >= 0);

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_gmv_nonnegative;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_gmv_nonnegative
  CHECK (
    gmv_usd_cents >= 0
    AND gmv_personal_usd_cents >= 0
    AND gmv_third_party_usd_cents >= 0
  );

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_gmv_split;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_gmv_split
  CHECK (gmv_personal_usd_cents + gmv_third_party_usd_cents = gmv_usd_cents);

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_frozen_at_required;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_frozen_at_required
  CHECK (status = 'open' OR frozen_at IS NOT NULL);

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_invoiced_at_required;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_invoiced_at_required
  CHECK (status <> 'invoiced' OR invoiced_at IS NOT NULL);

ALTER TABLE IF EXISTS monthly_brand_statements
  DROP CONSTRAINT IF EXISTS ck_monthly_brand_statements_calendar_month_start;
ALTER TABLE IF EXISTS monthly_brand_statements
  ADD CONSTRAINT ck_monthly_brand_statements_calendar_month_start
  CHECK (EXTRACT(DAY FROM calendar_month) = 1);
-- EXTRACT is IMMUTABLE for DATE; DATE_TRUNC(text, date) was historically only
-- STABLE in some Postgres minor versions, which Postgres rejects in CHECK.

CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_brand_statements_merchant_month
  ON monthly_brand_statements(merchant_id, calendar_month);
CREATE INDEX IF NOT EXISTS idx_monthly_brand_statements_month_status
  ON monthly_brand_statements(calendar_month, status);
CREATE INDEX IF NOT EXISTS idx_monthly_brand_statements_open_frozen
  ON monthly_brand_statements(status)
  WHERE status IN ('open', 'frozen');

CREATE OR REPLACE FUNCTION prevent_monthly_brand_statement_frozen_mutation()
RETURNS TRIGGER AS $$
BEGIN
  IF OLD.status = 'frozen' THEN
    IF NEW.status NOT IN ('frozen', 'invoiced') THEN
      RAISE EXCEPTION 'monthly_brand_statements frozen row % can only transition to invoiced', OLD.id;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
      OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
      OR NEW.calendar_month IS DISTINCT FROM OLD.calendar_month
      OR NEW.subscription_plan_id IS DISTINCT FROM OLD.subscription_plan_id
      OR NEW.tier_name IS DISTINCT FROM OLD.tier_name
      OR NEW.subscription_revenue_usd_cents IS DISTINCT FROM OLD.subscription_revenue_usd_cents
      OR NEW.credits_consumed IS DISTINCT FROM OLD.credits_consumed
      OR NEW.bundled_credits_consumed IS DISTINCT FROM OLD.bundled_credits_consumed
      OR NEW.overage_credits IS DISTINCT FROM OLD.overage_credits
      OR NEW.overage_revenue_usd_cents IS DISTINCT FROM OLD.overage_revenue_usd_cents
      OR NEW.gmv_usd_cents IS DISTINCT FROM OLD.gmv_usd_cents
      OR NEW.gmv_personal_usd_cents IS DISTINCT FROM OLD.gmv_personal_usd_cents
      OR NEW.gmv_third_party_usd_cents IS DISTINCT FROM OLD.gmv_third_party_usd_cents
      OR NEW.pivota_gmv_take_usd_cents IS DISTINCT FROM OLD.pivota_gmv_take_usd_cents
      OR NEW.total_revenue_usd_cents IS DISTINCT FROM OLD.total_revenue_usd_cents
      OR NEW.total_cogs_usd_cents IS DISTINCT FROM OLD.total_cogs_usd_cents
      OR NEW.pivota_gross_margin_usd_cents IS DISTINCT FROM OLD.pivota_gross_margin_usd_cents
      OR NEW.frozen_at IS DISTINCT FROM OLD.frozen_at
      OR NEW.metadata IS DISTINCT FROM OLD.metadata
      OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
      RAISE EXCEPTION 'monthly_brand_statements frozen row % does not allow computed field mutation', OLD.id;
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_monthly_brand_statements_freeze_guard ON monthly_brand_statements;
CREATE TRIGGER trg_monthly_brand_statements_freeze_guard
  BEFORE UPDATE ON monthly_brand_statements
  FOR EACH ROW EXECUTE PROCEDURE prevent_monthly_brand_statement_frozen_mutation();

DROP TRIGGER IF EXISTS trg_monthly_brand_statements_updated_at ON monthly_brand_statements;
CREATE TRIGGER trg_monthly_brand_statements_updated_at
  BEFORE UPDATE ON monthly_brand_statements
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

ALTER TABLE IF EXISTS billing_run_items
  DROP CONSTRAINT IF EXISTS ck_billing_run_items_source_type;
ALTER TABLE IF EXISTS billing_run_items
  ADD CONSTRAINT ck_billing_run_items_source_type CHECK (
    source_type IN (
      'gmv_rollup',
      'dispute_adj',
      'credit_note',
      'topup_refund',
      'subscription',
      'credit_overage'
    )
  );

COMMENT ON TABLE monthly_brand_statements IS
  'Canonical one-row-per-merchant-month monetization statement; internal COGS and gross-margin columns must not be serialized to merchant-facing endpoints.';
COMMENT ON COLUMN monthly_brand_statements.total_cogs_usd_cents IS
  'INTERNAL ONLY: blended SaaS and credit COGS for Pivota P&L.';
COMMENT ON COLUMN monthly_brand_statements.pivota_gross_margin_usd_cents IS
  'INTERNAL ONLY: Pivota gross margin for partner-settlement and CAC analysis.';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_monthly_brand_statements_freeze_guard ON monthly_brand_statements;
-- DROP TRIGGER IF EXISTS trg_monthly_brand_statements_updated_at ON monthly_brand_statements;
