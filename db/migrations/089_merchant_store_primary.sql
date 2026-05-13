ALTER TABLE merchant_stores
ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE merchant_stores ms
SET is_primary = TRUE
WHERE ms.store_id IN (
  SELECT DISTINCT ON (merchant_id) store_id
  FROM merchant_stores
  WHERE lower(COALESCE(status, '')) IN ('active', 'connected')
  ORDER BY merchant_id, connected_at DESC NULLS LAST, store_id DESC
)
AND NOT EXISTS (
  SELECT 1
  FROM merchant_stores existing
  WHERE existing.merchant_id = ms.merchant_id
    AND existing.is_primary = TRUE
    AND lower(COALESCE(existing.status, '')) IN ('active', 'connected')
);

WITH ranked_primary AS (
  SELECT
    store_id,
    ROW_NUMBER() OVER (
      PARTITION BY merchant_id
      ORDER BY connected_at DESC NULLS LAST, store_id DESC
    ) AS primary_rank
  FROM merchant_stores
  WHERE is_primary = TRUE
    AND lower(COALESCE(status, '')) IN ('active', 'connected')
)
UPDATE merchant_stores ms
SET is_primary = (ranked_primary.primary_rank = 1)
FROM ranked_primary
WHERE ms.store_id = ranked_primary.store_id;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_merchant_stores_primary_per_merchant
ON merchant_stores (merchant_id)
WHERE is_primary = TRUE
  AND lower(COALESCE(status, '')) IN ('active', 'connected');

CREATE INDEX IF NOT EXISTS idx_merchant_stores_merchant_primary
ON merchant_stores (merchant_id, is_primary, connected_at DESC);
