CREATE TABLE IF NOT EXISTS merchant_portal_preferences (
    merchant_id VARCHAR(50) PRIMARY KEY,
    email_orders BOOLEAN NOT NULL DEFAULT TRUE,
    email_payments BOOLEAN NOT NULL DEFAULT TRUE,
    email_inventory BOOLEAN NOT NULL DEFAULT FALSE,
    email_weekly BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
