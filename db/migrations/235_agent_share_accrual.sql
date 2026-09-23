-- ADR-025 D5: agent revenue share, as a percentage of what Pivota INVOICED for the agent's orders.
--
-- The same tables are declared in db/agent_share.py, and main.py's metadata.create_all creates them
-- at startup. This file is the reviewed DDL for environments that apply numbered migrations.
-- Keep the two in step.
--
-- agent_share_rates starts EMPTY: no rate for an agent means a share of 0. Nothing accrues until
-- a rate is set through services.agent_share_accrual.set_agent_share_rate, which is forward-only
-- and closes the open window under a per-agent advisory lock.

CREATE TABLE IF NOT EXISTS agent_share_rates (
  id BIGSERIAL PRIMARY KEY,
  agent_id VARCHAR(64) NOT NULL,
  share_bp INTEGER NOT NULL,
  effective_from TIMESTAMPTZ NOT NULL,
  effective_to TIMESTAMPTZ,
  created_by VARCHAR(128) NOT NULL,
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_agent_share_rates_share_bp CHECK (share_bp BETWEEN 0 AND 10000),
  CONSTRAINT ck_agent_share_rates_window CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE INDEX IF NOT EXISTS idx_agent_share_rates_agent_from
  ON agent_share_rates (agent_id, effective_from);

-- Append-only, one subject per invoiced line (billing_run_items, source_type 'gmv_rollup'). A line's
-- accrued share is SUM(amount_minor); corrections are signed rows.
CREATE TABLE IF NOT EXISTS agent_share_ledger (
  id BIGSERIAL PRIMARY KEY,
  billing_run_item_id BIGINT NOT NULL,
  rollup_id BIGINT NOT NULL,
  agent_id VARCHAR(64) NOT NULL,
  merchant_id VARCHAR(100) NOT NULL,
  currency VARCHAR(8) NOT NULL,
  entry_kind VARCHAR(16) NOT NULL,
  amount_minor BIGINT NOT NULL,
  billed_minor BIGINT NOT NULL,
  share_bp INTEGER NOT NULL,
  rate_id BIGINT,
  target_minor BIGINT NOT NULL,
  basis VARCHAR(32) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_agent_share_ledger_kind CHECK (entry_kind IN ('accrual', 'adjustment')),
  CONSTRAINT ck_agent_share_ledger_nonzero CHECK (amount_minor <> 0),
  CONSTRAINT ck_agent_share_ledger_target_nonneg CHECK (target_minor >= 0)
);

CREATE INDEX IF NOT EXISTS idx_agent_share_ledger_item ON agent_share_ledger (billing_run_item_id);
CREATE INDEX IF NOT EXISTS idx_agent_share_ledger_agent_created ON agent_share_ledger (agent_id, created_at);
