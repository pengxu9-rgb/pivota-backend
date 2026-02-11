-- 048_id_bridge.sql
-- Stable bridge from Aurora UUID identifiers to PDP subject contracts.

CREATE TABLE IF NOT EXISTS id_bridge (
  id BIGSERIAL PRIMARY KEY,
  bridge_key_type TEXT NOT NULL,
  bridge_key TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  product_group_id TEXT,
  merchant_id TEXT,
  product_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_id_bridge_key_type
    CHECK (bridge_key_type IN ('aurora_sku_uuid', 'aurora_product_uuid')),
  CONSTRAINT ck_id_bridge_subject_kind
    CHECK (subject_kind IN ('product_group', 'canonical_product')),
  CONSTRAINT ck_id_bridge_subject_shape
    CHECK (
      (subject_kind = 'product_group' AND product_group_id IS NOT NULL)
      OR
      (subject_kind = 'canonical_product' AND merchant_id IS NOT NULL AND product_id IS NOT NULL)
    ),
  CONSTRAINT uq_id_bridge_key UNIQUE (bridge_key_type, bridge_key)
);

CREATE INDEX IF NOT EXISTS idx_id_bridge_product_group_id
  ON id_bridge(product_group_id);

CREATE INDEX IF NOT EXISTS idx_id_bridge_canonical_ref
  ON id_bridge(merchant_id, product_id);

CREATE OR REPLACE FUNCTION update_id_bridge_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_id_bridge_updated_at ON id_bridge;
CREATE TRIGGER trg_id_bridge_updated_at
BEFORE UPDATE ON id_bridge
FOR EACH ROW
EXECUTE FUNCTION update_id_bridge_updated_at();
