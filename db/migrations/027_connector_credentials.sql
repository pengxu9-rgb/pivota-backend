-- Migration 027: Connector Credentials Table
-- Purpose: Store per-merchant connector credentials encrypted at rest

CREATE TABLE IF NOT EXISTS connector_credentials (
    id SERIAL PRIMARY KEY,
    
    -- Ownership
    merchant_id VARCHAR(50) NOT NULL,
    connector VARCHAR(50) NOT NULL,
    credential_label VARCHAR(100),
    
    -- Encrypted secret payload (e.g. Shopify shop_domain/access_token)
    credentials_encrypted TEXT NOT NULL,
    
    -- Validation and lifecycle
    is_valid BOOLEAN NOT NULL DEFAULT TRUE,
    last_validation_result JSONB,
    last_validated_at TIMESTAMP WITH TIME ZONE,
    last_used_at TIMESTAMP WITH TIME ZONE,
    
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_connector_credentials_merchant
    ON connector_credentials(merchant_id);

CREATE INDEX IF NOT EXISTS idx_connector_credentials_merchant_connector
    ON connector_credentials(merchant_id, connector);

-- Trigger to keep updated_at fresh
CREATE OR REPLACE FUNCTION update_connector_credentials_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_connector_credentials_updated_at
    BEFORE UPDATE ON connector_credentials
    FOR EACH ROW
    EXECUTE FUNCTION update_connector_credentials_updated_at();

