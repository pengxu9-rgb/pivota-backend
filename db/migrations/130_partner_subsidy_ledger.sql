-- 130_partner_subsidy_ledger.sql
-- Append-only onboarding subsidy ledger for channel partners.

CREATE TABLE IF NOT EXISTS partner_subsidy_ledger (
  id BIGSERIAL PRIMARY KEY,
  channel_partner_id BIGINT NOT NULL REFERENCES channel_partners(id) ON DELETE RESTRICT,
  merchant_id VARCHAR(50) NOT NULL,
  kind TEXT NOT NULL,
  amount_cents BIGINT NOT NULL,
  reference_id TEXT,
  notes TEXT,
  issued_by TEXT,
  issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_partner_subsidy_ledger_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_partner_subsidy_ledger_amount_positive CHECK (amount_cents > 0),
  CONSTRAINT ck_partner_subsidy_ledger_kind CHECK (kind IN (
    'waived_setup_fee',
    'complimentary_credits',
    'discounted_subscription',
    'co_funded_marketing'
  ))
);

CREATE INDEX IF NOT EXISTS idx_partner_subsidy_ledger_partner_brand
  ON partner_subsidy_ledger (channel_partner_id, merchant_id);
CREATE INDEX IF NOT EXISTS idx_partner_subsidy_ledger_issued_at
  ON partner_subsidy_ledger (issued_at DESC);

-- Append-only: block UPDATE / DELETE. Function reuses the existing
-- prevent_monetization_append_only_mutation() defined in migration 106.
DROP TRIGGER IF EXISTS trg_partner_subsidy_ledger_append_only
  ON partner_subsidy_ledger;
CREATE TRIGGER trg_partner_subsidy_ledger_append_only
  BEFORE UPDATE OR DELETE ON partner_subsidy_ledger
  FOR EACH ROW EXECUTE PROCEDURE prevent_monetization_append_only_mutation();

COMMENT ON TABLE partner_subsidy_ledger IS
  'Append-only audit log of onboarding subsidies issued per (partner × brand). Per-brand cap enforced by services/subsidy_service.issue() via partner row lock plus transaction-scoped advisory lock.';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_partner_subsidy_ledger_append_only ON partner_subsidy_ledger;
-- DROP TABLE IF EXISTS partner_subsidy_ledger;
