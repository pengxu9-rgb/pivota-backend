-- 136_catalog_row_trust_down.sql

DROP INDEX IF EXISTS idx_crt_reason_codes_gin;
DROP INDEX IF EXISTS idx_crt_policy_version_updated;
DROP INDEX IF EXISTS idx_crt_source_listing_ref;
DROP INDEX IF EXISTS idx_crt_product_key;
DROP INDEX IF EXISTS idx_crt_identity_status;
DROP INDEX IF EXISTS idx_crt_source_lifecycle_state;
DROP INDEX IF EXISTS idx_crt_serving_decision;

DROP TABLE IF EXISTS catalog_row_trust;
