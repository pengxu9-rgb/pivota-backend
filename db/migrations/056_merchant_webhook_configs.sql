CREATE TABLE IF NOT EXISTS merchant_webhook_configs (
    id SERIAL PRIMARY KEY,
    merchant_id VARCHAR(255) NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    destination_url TEXT,
    subscribed_events JSON NOT NULL DEFAULT '[]',
    signing_secret TEXT,
    last_test_at TIMESTAMP,
    last_test_status VARCHAR(32),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS merchant_webhook_deliveries (
    id SERIAL PRIMARY KEY,
    delivery_id VARCHAR(255) NOT NULL UNIQUE,
    merchant_id VARCHAR(255) NOT NULL,
    event_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    http_status INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMP,
    next_retry_at TIMESTAMP,
    request_id VARCHAR(255),
    destination_url TEXT,
    payload JSON NOT NULL DEFAULT '{}',
    request_headers JSON NOT NULL DEFAULT '{}',
    response_body TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_merchant_webhook_deliveries_merchant_created
ON merchant_webhook_deliveries(merchant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_merchant_webhook_deliveries_retry
ON merchant_webhook_deliveries(status, next_retry_at);
