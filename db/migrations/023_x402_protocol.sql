-- Migration 023: X-402 Protocol Support
-- Date: 2025-11-09
-- Purpose: Add schema for X-402 stablecoin payment protocol

-- X-402 transactions
CREATE TABLE IF NOT EXISTS x402_transactions (
    id SERIAL PRIMARY KEY,
    transaction_id VARCHAR(128) UNIQUE NOT NULL,
    order_id VARCHAR(50),
    agent_id VARCHAR(50) REFERENCES agents(agent_id) ON DELETE SET NULL,
    merchant_id VARCHAR(50) REFERENCES merchant_onboarding(merchant_id) ON DELETE SET NULL,
    amount DECIMAL(15,2) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    authorization_code VARCHAR(255) NOT NULL,
    wallet_network VARCHAR(50),
    wallet_address VARCHAR(255),
    tx_hash VARCHAR(255),
    status VARCHAR(32) CHECK (status IN ('authorized', 'captured', 'settled', 'refunded', 'voided', 'failed')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_x402_transactions_txn ON x402_transactions(transaction_id);
CREATE INDEX IF NOT EXISTS idx_x402_transactions_order ON x402_transactions(order_id);
CREATE INDEX IF NOT EXISTS idx_x402_transactions_agent ON x402_transactions(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_x402_transactions_merchant ON x402_transactions(merchant_id, created_at DESC);

COMMENT ON TABLE x402_transactions IS 'X-402 protocol transaction records';

-- Exchange rate snapshots
CREATE TABLE IF NOT EXISTS x402_exchange_rates (
    id SERIAL PRIMARY KEY,
    snapshot_id VARCHAR(100) UNIQUE NOT NULL,
    base_currency VARCHAR(3) NOT NULL,
    rates JSONB NOT NULL,
    provider VARCHAR(50),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_x402_exchange_rates_snapshot ON x402_exchange_rates(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_x402_exchange_rates_base ON x402_exchange_rates(base_currency, created_at DESC);

COMMENT ON TABLE x402_exchange_rates IS 'Exchange rate snapshots for X-402 multi-currency support';

-- X-402 support flag for merchants
ALTER TABLE merchant_onboarding ADD COLUMN IF NOT EXISTS x402_enabled BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN merchant_onboarding.x402_enabled IS 'Whether merchant accepts X-402 stablecoin payments';

-- Verification
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 023 completed - X-402 protocol ready';
END $$;










