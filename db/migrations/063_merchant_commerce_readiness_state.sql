CREATE TABLE IF NOT EXISTS merchant_commerce_readiness_state (
    merchant_id VARCHAR(50) PRIMARY KEY,
    primary_platform VARCHAR(32),
    active_psp VARCHAR(64),
    foundation_status VARCHAR(32) NOT NULL,
    discover_status VARCHAR(32) NOT NULL,
    signals_status VARCHAR(32) NOT NULL,
    execute_status VARCHAR(32) NOT NULL,
    foundation_blockers JSONB,
    discover_blockers JSONB,
    signals_blockers JSONB,
    execute_blockers JSONB,
    surfaced_exposure_supported BOOLEAN NOT NULL DEFAULT FALSE,
    first_store_connected_at TIMESTAMPTZ,
    first_catalog_synced_at TIMESTAMPTZ,
    first_discover_ready_at TIMESTAMPTZ,
    days_to_discover_ready INTEGER,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merchant_commerce_readiness_platform ON merchant_commerce_readiness_state(primary_platform);
CREATE INDEX IF NOT EXISTS idx_merchant_commerce_readiness_foundation ON merchant_commerce_readiness_state(foundation_status);
CREATE INDEX IF NOT EXISTS idx_merchant_commerce_readiness_discover ON merchant_commerce_readiness_state(discover_status);
CREATE INDEX IF NOT EXISTS idx_merchant_commerce_readiness_signals ON merchant_commerce_readiness_state(signals_status);
CREATE INDEX IF NOT EXISTS idx_merchant_commerce_readiness_execute ON merchant_commerce_readiness_state(execute_status);
CREATE INDEX IF NOT EXISTS idx_merchant_commerce_readiness_observed ON merchant_commerce_readiness_state(observed_at);
