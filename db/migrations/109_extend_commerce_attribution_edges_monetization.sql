-- 109_extend_commerce_attribution_edges_monetization.sql
-- Extend commerce attribution edges with partner, take-rate, refund, and protocol billing fields.

ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS channel_partner_id BIGINT,
  ADD COLUMN IF NOT EXISTS take_rate_applied_bp SMALLINT,
  ADD COLUMN IF NOT EXISTS gross_attributed_gmv_cents BIGINT,
  ADD COLUMN IF NOT EXISTS refund_amount_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS protocol_name TEXT;

ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS net_attributed_gmv_cents BIGINT GENERATED ALWAYS AS (
    CASE
      WHEN gross_attributed_gmv_cents IS NULL THEN NULL
      ELSE GREATEST(gross_attributed_gmv_cents - refund_amount_cents, 0)
    END
  ) STORED;

ALTER TABLE IF EXISTS commerce_attribution_edges
  ALTER COLUMN protocol_name TYPE TEXT;

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS commerce_attribution_edges_channel_partner_id_fkey;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT commerce_attribution_edges_channel_partner_id_fkey
  FOREIGN KEY (channel_partner_id) REFERENCES channel_partners(id) ON DELETE SET NULL;

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_take_rate_applied_bp;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_take_rate_applied_bp
  CHECK (take_rate_applied_bp IS NULL OR take_rate_applied_bp BETWEEN 0 AND 10000);

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_gross_attributed_gmv_cents;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_gross_attributed_gmv_cents
  CHECK (gross_attributed_gmv_cents IS NULL OR gross_attributed_gmv_cents >= 0);

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_refund_amount_cents;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_refund_amount_cents
  CHECK (refund_amount_cents >= 0);

CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_channel_partner_id
  ON commerce_attribution_edges(channel_partner_id);
CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_channel_partner_created
  ON commerce_attribution_edges(channel_partner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_refunded_at
  ON commerce_attribution_edges(refunded_at);
CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_gmv_cents
  ON commerce_attribution_edges(gross_attributed_gmv_cents, refund_amount_cents, net_attributed_gmv_cents);

COMMENT ON COLUMN commerce_attribution_edges.channel_partner_id IS 'Channel partner credited for this commerce attribution edge';
COMMENT ON COLUMN commerce_attribution_edges.take_rate_applied_bp IS 'Take rate applied in basis points, e.g. 500 promo or 1000 standard';
COMMENT ON COLUMN commerce_attribution_edges.gross_attributed_gmv_cents IS 'Gross attributed GMV in cents before refunds';
COMMENT ON COLUMN commerce_attribution_edges.refund_amount_cents IS 'Attributed refund amount in cents';
COMMENT ON COLUMN commerce_attribution_edges.net_attributed_gmv_cents IS 'Computed gross minus refund, floored at zero';
COMMENT ON COLUMN commerce_attribution_edges.protocol_name IS 'Protocol tag logged for v1 and reserved for v1.5 routing';

-- DOWN (manual rollback only; repo startup executes this file as UP-only):
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP CONSTRAINT IF EXISTS commerce_attribution_edges_channel_partner_id_fkey;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_take_rate_applied_bp;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_gross_attributed_gmv_cents;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_refund_amount_cents;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS net_attributed_gmv_cents;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS protocol_name;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS refunded_at;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS refund_amount_cents;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS gross_attributed_gmv_cents;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS take_rate_applied_bp;
-- ALTER TABLE IF EXISTS commerce_attribution_edges DROP COLUMN IF EXISTS channel_partner_id;
