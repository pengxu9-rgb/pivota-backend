-- Preserve exact fractional model usage; no conversion of existing values.
SET LOCAL lock_timeout = '5s';
ALTER TABLE merchant_credit_balance
  ALTER COLUMN credits TYPE NUMERIC(24,8),
  ALTER COLUMN purchased_credits TYPE NUMERIC(24,8),
  ALTER COLUMN allowance_credits TYPE NUMERIC(24,8),
  ALTER COLUMN overage_pending_credits TYPE NUMERIC(24,8),
  ALTER COLUMN overage_charged_credits TYPE NUMERIC(24,8),
  ALTER COLUMN usd_cogs_internal TYPE NUMERIC(24,12);
ALTER TABLE agent_center_usage_events ALTER COLUMN quantity TYPE NUMERIC(24,8);
ALTER TABLE monthly_brand_statements
  ALTER COLUMN credits_consumed TYPE NUMERIC(24,8),
  ALTER COLUMN bundled_credits_consumed TYPE NUMERIC(24,8),
  ALTER COLUMN overage_credits TYPE NUMERIC(24,8);
CREATE TABLE IF NOT EXISTS merchant_llm_operations (
  merchant_id TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  operation_type TEXT NOT NULL,
  result_jsonb JSONB NOT NULL,
  usage_jsonb JSONB NOT NULL,
  credits NUMERIC(24,8) NOT NULL CHECK (credits >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (merchant_id, operation_key)
);

-- Lifecycle corrections are not model usage and must remain separately auditable.
CREATE TABLE IF NOT EXISTS merchant_credit_adjustments (
  merchant_id TEXT NOT NULL,
  previous_version BIGINT NOT NULL,
  reason TEXT NOT NULL,
  credits_before NUMERIC(24,8) NOT NULL,
  credits_after NUMERIC(24,8) NOT NULL,
  previous_plan_tier TEXT NOT NULL,
  previous_allowance_period TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (merchant_id, previous_version)
);

-- Enable only after web and worker both understand fractional refund amounts.
CREATE TABLE IF NOT EXISTS merchant_metering_controls (
  policy_version TEXT PRIMARY KEY,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO merchant_metering_controls (policy_version, enabled)
VALUES ('measured_text_v1', FALSE) ON CONFLICT DO NOTHING;
