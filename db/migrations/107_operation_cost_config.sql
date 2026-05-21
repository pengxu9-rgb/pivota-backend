-- 107_operation_cost_config.sql
-- Versioned operation type to credit cost configuration.

CREATE TABLE IF NOT EXISTS operation_cost_config (
  id BIGSERIAL PRIMARY KEY,
  operation_type TEXT NOT NULL,
  base_cost_credits BIGINT NOT NULL,
  tier_multipliers_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  version INTEGER NOT NULL,
  effective_from TIMESTAMPTZ NOT NULL,
  effective_until TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_operation_cost_config_operation_version UNIQUE (operation_type, version),
  CONSTRAINT ck_operation_cost_config_operation_type_nonempty CHECK (operation_type <> ''),
  CONSTRAINT ck_operation_cost_config_base_cost_nonnegative CHECK (base_cost_credits >= 0),
  CONSTRAINT ck_operation_cost_config_version_positive CHECK (version > 0),
  CONSTRAINT ck_operation_cost_config_status CHECK (status IN ('draft', 'active', 'retired')),
  CONSTRAINT ck_operation_cost_config_effective_order CHECK (
    effective_until IS NULL OR effective_until > effective_from
  )
);

CREATE INDEX IF NOT EXISTS idx_operation_cost_config_operation_type
  ON operation_cost_config(operation_type);
CREATE INDEX IF NOT EXISTS idx_operation_cost_config_effective_from
  ON operation_cost_config(effective_from DESC);
CREATE INDEX IF NOT EXISTS idx_operation_cost_config_status
  ON operation_cost_config(status);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_operation_cost_config_updated_at ON operation_cost_config;
CREATE TRIGGER trg_operation_cost_config_updated_at
  BEFORE UPDATE ON operation_cost_config
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE operation_cost_config IS 'Monetization v1.3: versioned operation type to credit cost mapping';
COMMENT ON COLUMN operation_cost_config.tier_multipliers_json IS 'Per-tier multipliers keyed by free/starter/growth/scale';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_operation_cost_config_updated_at ON operation_cost_config;
-- DROP TABLE IF EXISTS operation_cost_config;
