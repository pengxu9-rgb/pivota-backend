-- 128_gmv_channel_classification.sql
-- Per-order GMV channel classification on commerce attribution edges.
--
-- No backfill in this migration. Existing rows keep gmv_channel NULL until
-- operators classify verified attribution data via the internal service.

ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS gmv_channel TEXT,
  ADD COLUMN IF NOT EXISTS third_party_platform TEXT,
  ADD COLUMN IF NOT EXISTS third_party_platform_fee_pct NUMERIC(5,4);

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_gmv_channel;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_gmv_channel
  CHECK (gmv_channel IS NULL OR gmv_channel IN ('personal_agent', 'third_party_agent'));

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_third_party_platform_channel;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_third_party_platform_channel
  CHECK (third_party_platform IS NULL OR gmv_channel = 'third_party_agent');

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_platform_requires_classified_channel;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_platform_requires_classified_channel
  CHECK (third_party_platform IS NULL OR gmv_channel IS NOT NULL);

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_third_party_platform_nonempty;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_third_party_platform_nonempty
  CHECK (third_party_platform IS NULL OR LENGTH(BTRIM(third_party_platform)) > 0);

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_third_party_fee_pct_range;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_third_party_fee_pct_range
  CHECK (
    third_party_platform_fee_pct IS NULL
    OR (
      third_party_platform_fee_pct >= 0
      AND third_party_platform_fee_pct <= 1
    )
  );

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_third_party_platform_fee_pair;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_third_party_platform_fee_pair
  CHECK ((third_party_platform IS NULL) = (third_party_platform_fee_pct IS NULL));

ALTER TABLE IF EXISTS commerce_attribution_edges
  DROP CONSTRAINT IF EXISTS ck_commerce_attribution_edges_third_party_requires_platform;
ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD CONSTRAINT ck_commerce_attribution_edges_third_party_requires_platform
  CHECK (gmv_channel IS NULL OR gmv_channel <> 'third_party_agent' OR third_party_platform IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_gmv_channel
  ON commerce_attribution_edges(gmv_channel);

COMMENT ON COLUMN commerce_attribution_edges.gmv_channel IS
  'GMV channel classification for attributed brand sales. NULL means unclassified and excluded from PR #4 statement GMV totals.';
COMMENT ON COLUMN commerce_attribution_edges.third_party_platform IS
  'Third-party platform name for third_party_agent GMV edges, e.g. openai. NULL for personal_agent.';
COMMENT ON COLUMN commerce_attribution_edges.third_party_platform_fee_pct IS
  'Decimal fraction of Pivota GMV-take paid to the third-party platform, e.g. 0.6500 for 65%. NULL for personal_agent.';
