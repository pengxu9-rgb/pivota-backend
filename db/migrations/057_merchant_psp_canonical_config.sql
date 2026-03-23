-- Canonical merchant PSP configuration fields
ALTER TABLE merchant_psps
    ADD COLUMN IF NOT EXISTS secret_key TEXT,
    ADD COLUMN IF NOT EXISTS environment VARCHAR(20) DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS provider_config JSONB DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS validation_status VARCHAR(20) DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS validation_error TEXT,
    ADD COLUMN IF NOT EXISTS last_validated_at TIMESTAMP WITH TIME ZONE;

UPDATE merchant_psps
SET
    environment = COALESCE(NULLIF(environment, ''), 'unknown'),
    provider_config = COALESCE(provider_config, '{}'::jsonb),
    validation_status = COALESCE(NULLIF(validation_status, ''), 'unknown')
WHERE TRUE;

CREATE INDEX IF NOT EXISTS idx_merchant_psps_provider_status
    ON merchant_psps(merchant_id, provider, status);
