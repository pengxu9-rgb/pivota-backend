-- 110_gmv_attribution_daily.sql
-- Daily gross/refund/net GMV rollups for GMV-take billing.

CREATE TABLE IF NOT EXISTS gmv_attribution_daily (
  id BIGSERIAL PRIMARY KEY,
  date DATE NOT NULL,
  merchant_id VARCHAR(50) NOT NULL,
  agent_id VARCHAR(64),
  channel_partner_id BIGINT REFERENCES channel_partners(id) ON DELETE SET NULL,
  gross_attributed_gmv_cents BIGINT NOT NULL DEFAULT 0,
  refund_amount_cents BIGINT NOT NULL DEFAULT 0,
  net_attributed_gmv_cents BIGINT NOT NULL DEFAULT 0,
  take_rate_bp SMALLINT NOT NULL DEFAULT 0,
  take_amount_cents BIGINT NOT NULL DEFAULT 0,
  protocol_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_gmv_attribution_daily_merchant_id_nonempty CHECK (merchant_id <> ''),
  CONSTRAINT ck_gmv_attribution_daily_gross_nonnegative CHECK (gross_attributed_gmv_cents >= 0),
  CONSTRAINT ck_gmv_attribution_daily_refund_nonnegative CHECK (refund_amount_cents >= 0),
  CONSTRAINT ck_gmv_attribution_daily_net_nonnegative CHECK (net_attributed_gmv_cents >= 0),
  CONSTRAINT ck_gmv_attribution_daily_take_rate_bp CHECK (take_rate_bp BETWEEN 0 AND 10000),
  CONSTRAINT ck_gmv_attribution_daily_take_amount_nonnegative CHECK (take_amount_cents >= 0)
);

-- Expression-based unique index: COALESCE handles NULLs so ON CONFLICT works when
-- agent_id or channel_partner_id is absent (Postgres UNIQUE treats NULLs as distinct,
-- which would silently allow duplicate rows and break UPSERT idempotency in T6).
CREATE UNIQUE INDEX IF NOT EXISTS uq_gmv_attribution_daily_rollup
  ON gmv_attribution_daily (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1));

CREATE INDEX IF NOT EXISTS idx_gmv_attribution_daily_merchant_date
  ON gmv_attribution_daily(merchant_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_gmv_attribution_daily_rollup
  ON gmv_attribution_daily(date, merchant_id, agent_id, channel_partner_id);
CREATE INDEX IF NOT EXISTS idx_gmv_attribution_daily_agent_date
  ON gmv_attribution_daily(agent_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_gmv_attribution_daily_channel_partner_id
  ON gmv_attribution_daily(channel_partner_id);
CREATE INDEX IF NOT EXISTS idx_gmv_attribution_daily_partner_date
  ON gmv_attribution_daily(channel_partner_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_gmv_attribution_daily_protocol_date
  ON gmv_attribution_daily(protocol_name, date DESC);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_gmv_attribution_daily_updated_at ON gmv_attribution_daily;
CREATE TRIGGER trg_gmv_attribution_daily_updated_at
  BEFORE UPDATE ON gmv_attribution_daily
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE gmv_attribution_daily IS 'Monetization v1.3: daily attributed GMV rollup net of refunds';
COMMENT ON INDEX uq_gmv_attribution_daily_rollup IS 'UPSERT idempotency key; expression index with COALESCE so ON CONFLICT resolves correctly when agent_id or channel_partner_id is NULL';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- DROP TRIGGER IF EXISTS trg_gmv_attribution_daily_updated_at ON gmv_attribution_daily;
-- DROP INDEX IF EXISTS uq_gmv_attribution_daily_rollup;
-- DROP TABLE IF EXISTS gmv_attribution_daily;
