-- Migration 022: Wallet Infrastructure for X-402
-- Date: 2025-11-09
-- Purpose: Add wallet management for cryptocurrency payments

-- Merchant crypto wallets
CREATE TABLE IF NOT EXISTS merchant_wallets (
    wallet_id VARCHAR(100) PRIMARY KEY,
    merchant_id VARCHAR(50) REFERENCES merchant_onboarding(merchant_id) ON DELETE CASCADE,
    network VARCHAR(50) NOT NULL,
    address VARCHAR(255) NOT NULL,
    custodian VARCHAR(50),
    custodian_account_id VARCHAR(255),
    status VARCHAR(20) CHECK (status IN ('pending', 'active', 'inactive')) DEFAULT 'pending',
    verified_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metadata JSONB,
    UNIQUE(merchant_id, network, address)
);

CREATE INDEX IF NOT EXISTS idx_merchant_wallets_merchant ON merchant_wallets(merchant_id, status);
CREATE INDEX IF NOT EXISTS idx_merchant_wallets_network ON merchant_wallets(network, status);

COMMENT ON TABLE merchant_wallets IS 'Merchant cryptocurrency wallet addresses for X-402 payments';

-- Agent crypto wallets
CREATE TABLE IF NOT EXISTS agent_wallets (
    wallet_id VARCHAR(100) PRIMARY KEY,
    agent_id VARCHAR(50) REFERENCES agents(agent_id) ON DELETE CASCADE,
    network VARCHAR(50) NOT NULL,
    address VARCHAR(255) NOT NULL,
    custodian VARCHAR(50),
    custodian_account_id VARCHAR(255),
    status VARCHAR(20) CHECK (status IN ('pending', 'active', 'inactive')) DEFAULT 'pending',
    verified_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metadata JSONB,
    UNIQUE(agent_id, network, address)
);

CREATE INDEX IF NOT EXISTS idx_agent_wallets_agent ON agent_wallets(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_wallets_network ON agent_wallets(network, status);

COMMENT ON TABLE agent_wallets IS 'Agent cryptocurrency wallet addresses for X-402 payments';

-- Wallet verification logs
CREATE TABLE IF NOT EXISTS wallet_verification_logs (
    log_id VARCHAR(100) PRIMARY KEY,
    wallet_id VARCHAR(100),
    verification_method VARCHAR(50),
    result VARCHAR(20),
    details JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wallet_verification_logs_wallet ON wallet_verification_logs(wallet_id, created_at DESC);

COMMENT ON TABLE wallet_verification_logs IS 'Audit log for wallet verification attempts';

-- Verification
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 022 completed - Wallet infrastructure ready';
END $$;

