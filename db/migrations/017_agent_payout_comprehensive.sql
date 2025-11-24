-- ============================================================================
-- Migration 017: Comprehensive Agent Payout Information
-- ============================================================================
-- Purpose: Collect agent payment details for global payout support
-- Created: 2025-11-04
-- Phase: 6.1 - Payout System
-- ============================================================================

-- ============================================================================
-- Part 1: agent_payout_settings - Store payout preferences and details
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_payout_settings (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    
    -- Payout Method Preferences (can have multiple, priority order)
    primary_payout_method VARCHAR(30) NOT NULL,
    -- 'stripe_connect', 'paypal', 'bank_transfer_us', 'bank_transfer_international', 
    -- 'wire_transfer', 'crypto', 'check', 'manual'
    
    backup_payout_method VARCHAR(30),
    
    -- ========================================================================
    -- STRIPE CONNECT (Global - Easiest)
    -- ========================================================================
    stripe_account_id VARCHAR(100) UNIQUE,
    stripe_onboarding_complete BOOLEAN DEFAULT false,
    stripe_payouts_enabled BOOLEAN DEFAULT false,
    stripe_capabilities JSON,  -- Store enabled capabilities
    stripe_country VARCHAR(3),  -- Country code from Stripe
    stripe_connected_at TIMESTAMP WITH TIME ZONE,
    
    -- ========================================================================
    -- PAYPAL (Global - Simple)
    -- ========================================================================
    paypal_email VARCHAR(255),
    paypal_verified BOOLEAN DEFAULT false,
    paypal_verified_at TIMESTAMP WITH TIME ZONE,
    
    -- ========================================================================
    -- BANK TRANSFER - US (ACH)
    -- ========================================================================
    us_bank_account_holder_name VARCHAR(255),
    us_bank_account_number_encrypted TEXT,  -- Encrypted
    us_bank_routing_number_encrypted TEXT,  -- Encrypted
    us_bank_account_type VARCHAR(20),  -- 'checking', 'savings'
    us_bank_name VARCHAR(255),
    us_bank_verified BOOLEAN DEFAULT false,
    us_bank_verification_method VARCHAR(50),  -- 'plaid', 'micro_deposit', 'manual'
    
    -- ========================================================================
    -- BANK TRANSFER - International (SWIFT/IBAN)
    -- ========================================================================
    intl_bank_account_holder_name VARCHAR(255),
    intl_iban VARCHAR(34),  -- International Bank Account Number (Europe, etc.)
    intl_swift_bic VARCHAR(11),  -- SWIFT/BIC code
    intl_bank_name VARCHAR(255),
    intl_bank_address TEXT,
    intl_bank_country VARCHAR(3),  -- ISO country code
    intl_account_currency VARCHAR(3) DEFAULT 'USD',
    intl_intermediary_bank_info TEXT,  -- For some countries
    intl_bank_verified BOOLEAN DEFAULT false,
    
    -- ========================================================================
    -- WIRE TRANSFER (High value, international)
    -- ========================================================================
    wire_beneficiary_name VARCHAR(255),
    wire_beneficiary_address TEXT,
    wire_bank_name VARCHAR(255),
    wire_bank_address TEXT,
    wire_account_number_encrypted TEXT,
    wire_swift_code VARCHAR(11),
    wire_iban VARCHAR(34),
    wire_routing_number VARCHAR(20),
    wire_reference_required TEXT,  -- Some banks need specific reference
    
    -- ========================================================================
    -- CRYPTOCURRENCY (Modern, global)
    -- ========================================================================
    crypto_wallet_address VARCHAR(255),
    crypto_network VARCHAR(50),  -- 'ethereum', 'polygon', 'bsc', 'tron'
    crypto_preferred_stablecoin VARCHAR(20),  -- 'USDC', 'USDT', 'DAI'
    crypto_verified BOOLEAN DEFAULT false,
    
    -- ========================================================================
    -- CHECK/CHEQUE (Legacy, some countries)
    -- ========================================================================
    check_mailing_address TEXT,
    check_recipient_name VARCHAR(255),
    
    -- ========================================================================
    -- TAX & COMPLIANCE (Required for all)
    -- ========================================================================
    tax_country VARCHAR(3) NOT NULL,  -- Agent's tax residency
    tax_id_number_encrypted TEXT,  -- SSN/EIN (US), VAT (EU), etc - Encrypted
    tax_id_type VARCHAR(50),  -- 'ssn', 'ein', 'vat', 'national_id'
    business_type VARCHAR(50),  -- 'individual', 'sole_proprietor', 'llc', 'corporation', 'partnership'
    business_legal_name VARCHAR(255),
    business_registration_number VARCHAR(100),
    
    -- W9/W8 forms for US tax compliance
    w9_form_url TEXT,  -- S3/storage URL
    w8_form_url TEXT,  -- For non-US agents
    w9_submitted_at TIMESTAMP WITH TIME ZONE,
    w8_submitted_at TIMESTAMP WITH TIME ZONE,
    
    -- ========================================================================
    -- PAYOUT PREFERENCES
    -- ========================================================================
    minimum_payout_amount DECIMAL(10,2) DEFAULT 50.00,
    payout_frequency VARCHAR(20) DEFAULT 'monthly',
    -- 'weekly', 'bi_weekly', 'monthly', 'quarterly', 'on_demand'
    
    preferred_currency VARCHAR(3) DEFAULT 'USD',
    currency_conversion_accepted BOOLEAN DEFAULT true,
    
    -- Hold earnings until minimum reached
    auto_payout_enabled BOOLEAN DEFAULT true,
    hold_payouts BOOLEAN DEFAULT false,  -- For disputes/issues
    
    -- ========================================================================
    -- VERIFICATION & COMPLIANCE
    -- ========================================================================
    verification_status VARCHAR(20) DEFAULT 'pending',
    -- 'pending', 'documents_required', 'under_review', 'verified', 
    -- 'rejected', 'suspended', 'inactive'
    
    verified_at TIMESTAMP WITH TIME ZONE,
    verified_by VARCHAR(100),  -- Admin who verified
    
    kyc_status VARCHAR(20) DEFAULT 'pending',
    -- 'pending', 'approved', 'rejected', 'needs_update'
    
    kyc_documents JSON,  -- URLs to ID, proof of address, etc
    -- {
    --   "id_document": "s3://...",
    --   "proof_of_address": "s3://...",
    --   "business_registration": "s3://..."
    -- }
    
    verification_notes TEXT,
    rejection_reason TEXT,
    
    -- ========================================================================
    -- METADATA & TRACKING
    -- ========================================================================
    last_payout_date TIMESTAMP WITH TIME ZONE,
    total_payouts_count INTEGER DEFAULT 0,
    total_paid_out DECIMAL(15,2) DEFAULT 0,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(agent_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_agent_payout_agent ON agent_payout_settings(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_payout_verification ON agent_payout_settings(verification_status);
CREATE INDEX IF NOT EXISTS idx_agent_payout_method ON agent_payout_settings(primary_payout_method);
CREATE INDEX IF NOT EXISTS idx_agent_payout_country ON agent_payout_settings(tax_country);

-- Comments
COMMENT ON TABLE agent_payout_settings IS '[Phase 6.1] Comprehensive agent payout information supporting multiple methods and countries';
COMMENT ON COLUMN agent_payout_settings.primary_payout_method IS 'Primary payout method: stripe_connect, paypal, bank_transfer_us, bank_transfer_international, wire_transfer, crypto, check';
COMMENT ON COLUMN agent_payout_settings.tax_id_number_encrypted IS 'Encrypted tax ID (SSN/EIN/VAT/etc) for compliance';
COMMENT ON COLUMN agent_payout_settings.verification_status IS 'KYC/verification status before payouts can be processed';

-- ============================================================================
-- Part 2: payout_transactions - Track individual payout executions
-- ============================================================================

CREATE TABLE IF NOT EXISTS payout_transactions (
    id SERIAL PRIMARY KEY,
    payout_id VARCHAR(50) UNIQUE NOT NULL,
    settlement_id VARCHAR(50) NOT NULL,  -- Links to agent_settlements
    agent_id VARCHAR(50) NOT NULL REFERENCES agents(agent_id),
    
    -- Payout details
    amount DECIMAL(15,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD',
    payout_method VARCHAR(30) NOT NULL,
    
    -- External references (from payment provider)
    external_transaction_id VARCHAR(255),  -- Stripe transfer ID, PayPal batch ID, etc
    external_status VARCHAR(50),
    
    -- Status tracking
    status VARCHAR(20) DEFAULT 'pending',
    -- 'pending', 'processing', 'completed', 'failed', 'cancelled', 'reversed'
    
    initiated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    failed_at TIMESTAMP WITH TIME ZONE,
    
    -- Error handling
    error_code VARCHAR(50),
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    
    -- Fee tracking
    payout_fee DECIMAL(10,2),  -- Fee charged by payment provider
    net_amount DECIMAL(15,2),  -- Amount agent actually receives
    
    -- Reconciliation
    reconciled BOOLEAN DEFAULT false,
    reconciled_at TIMESTAMP WITH TIME ZONE,
    reconciled_by VARCHAR(100),
    
    -- Metadata
    metadata JSON,
    notes TEXT,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_payout_transactions_agent ON payout_transactions(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_payout_transactions_settlement ON payout_transactions(settlement_id);
CREATE INDEX IF NOT EXISTS idx_payout_transactions_status ON payout_transactions(status, initiated_at);
CREATE INDEX IF NOT EXISTS idx_payout_transactions_external ON payout_transactions(external_transaction_id);

COMMENT ON TABLE payout_transactions IS '[Phase 6.1] Individual payout transaction records with provider integration';
COMMENT ON COLUMN payout_transactions.external_transaction_id IS 'Transaction ID from payment provider (Stripe, PayPal, bank, etc)';
COMMENT ON COLUMN payout_transactions.payout_fee IS 'Fee charged by payment provider for the transfer';

-- ============================================================================
-- Part 3: payout_method_availability - Track which methods are available per country
-- ============================================================================

CREATE TABLE IF NOT EXISTS payout_method_availability (
    id SERIAL PRIMARY KEY,
    country_code VARCHAR(3) NOT NULL,  -- ISO country code
    payout_method VARCHAR(30) NOT NULL,
    is_available BOOLEAN DEFAULT true,
    min_amount DECIMAL(10,2),  -- Minimum payout for this method in this country
    max_amount DECIMAL(15,2),  -- Maximum payout (if any)
    processing_days INTEGER,  -- Estimated days to receive
    fee_percentage DECIMAL(5,4),  -- Fee as percentage
    fee_fixed DECIMAL(10,2),  -- Fixed fee per transaction
    currency VARCHAR(3),  -- Local currency
    notes TEXT,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(country_code, payout_method)
);

COMMENT ON TABLE payout_method_availability IS 'Defines which payout methods are available in each country with fees and limits';

-- Seed common payout method availability
INSERT INTO payout_method_availability (country_code, payout_method, min_amount, processing_days, fee_percentage, currency) VALUES
-- United States
('USA', 'stripe_connect', 1.00, 2, 0.0025, 'USD'),
('USA', 'paypal', 1.00, 1, 0.02, 'USD'),
('USA', 'bank_transfer_us', 50.00, 3, 0.0, 'USD'),
('USA', 'wire_transfer', 1000.00, 1, 25.00, 'USD'),

-- Europe
('GBR', 'stripe_connect', 1.00, 2, 0.0025, 'GBP'),
('GBR', 'paypal', 1.00, 1, 0.02, 'GBP'),
('GBR', 'bank_transfer_international', 50.00, 5, 0.0, 'GBP'),

('DEU', 'stripe_connect', 1.00, 2, 0.0025, 'EUR'),
('DEU', 'bank_transfer_international', 50.00, 3, 0.0, 'EUR'),

-- Asia
('CHN', 'wire_transfer', 100.00, 5, 30.00, 'CNY'),
('CHN', 'crypto', 10.00, 1, 0.01, 'USD'),

('JPN', 'stripe_connect', 1.00, 2, 0.0025, 'JPY'),
('JPN', 'bank_transfer_international', 50.00, 3, 0.0, 'JPY'),

-- Global fallback
('GLOBAL', 'stripe_connect', 1.00, 2, 0.0025, 'USD'),
('GLOBAL', 'paypal', 1.00, 1, 0.02, 'USD'),
('GLOBAL', 'wire_transfer', 1000.00, 7, 35.00, 'USD'),
('GLOBAL', 'crypto', 10.00, 1, 0.01, 'USD')

ON CONFLICT (country_code, payout_method) DO NOTHING;

-- ============================================================================
-- Part 4: agent_payout_history - Audit log of all payout attempts
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_payout_history (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL REFERENCES agents(agent_id),
    action VARCHAR(50) NOT NULL,
    -- 'settings_updated', 'method_added', 'method_verified', 'method_failed',
    -- 'payout_initiated', 'payout_completed', 'payout_failed', 'settings_changed'
    
    payout_method VARCHAR(30),
    old_value JSON,
    new_value JSON,
    
    performed_by VARCHAR(100),  -- Who made the change (agent or admin)
    ip_address VARCHAR(45),
    user_agent TEXT,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_payout_history_agent ON agent_payout_history(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_payout_history_action ON agent_payout_history(action);

COMMENT ON TABLE agent_payout_history IS 'Audit log of all payout setting changes and payout attempts';

-- ============================================================================
-- Part 5: Update agent_settlements with payout tracking
-- ============================================================================

-- Add payout tracking to settlements if columns don't exist
DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                   WHERE table_name='agent_settlements' AND column_name='payout_transaction_id') THEN
        ALTER TABLE agent_settlements 
        ADD COLUMN payout_transaction_id VARCHAR(50),
        ADD COLUMN payout_fee DECIMAL(10,2) DEFAULT 0,
        ADD COLUMN payout_net_amount DECIMAL(12,2);
    END IF;
END $$;

COMMENT ON COLUMN agent_settlements.payout_transaction_id IS 'Links to payout_transactions table for actual payout execution';

-- ============================================================================
-- Part 6: Verification & Security Views
-- ============================================================================

-- View for agents pending verification
CREATE OR REPLACE VIEW agents_pending_payout_verification AS
SELECT 
    a.agent_id,
    a.name,
    a.email,
    a.company,
    a.agent_type,
    aps.primary_payout_method,
    aps.verification_status,
    aps.kyc_status,
    aps.tax_country,
    aps.created_at as settings_created_at,
    aps.stripe_onboarding_complete,
    aps.paypal_verified,
    aps.us_bank_verified,
    aps.intl_bank_verified
FROM agents a
INNER JOIN agent_payout_settings aps ON a.agent_id = aps.agent_id
WHERE aps.verification_status IN ('pending', 'documents_required', 'under_review')
ORDER BY aps.created_at ASC;

COMMENT ON VIEW agents_pending_payout_verification IS 'Agents waiting for payout verification - for admin review';

-- View for payout-ready agents
CREATE OR REPLACE VIEW agents_payout_ready AS
SELECT 
    a.agent_id,
    a.name,
    a.email,
    aps.primary_payout_method,
    aps.tax_country,
    aps.minimum_payout_amount,
    aps.preferred_currency,
    aps.last_payout_date,
    aps.total_payouts_count,
    aps.total_paid_out
FROM agents a
INNER JOIN agent_payout_settings aps ON a.agent_id = aps.agent_id
WHERE aps.verification_status = 'verified'
AND aps.hold_payouts = false
AND a.status = 'active'
ORDER BY a.name;

COMMENT ON VIEW agents_payout_ready IS 'Agents ready to receive payouts - verified and active';

-- ============================================================================
-- Verification
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_payout_settings') THEN
        RAISE NOTICE '[Phase 6.1] ✅ agent_payout_settings table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'payout_transactions') THEN
        RAISE NOTICE '[Phase 6.1] ✅ payout_transactions table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'payout_method_availability') THEN
        RAISE NOTICE '[Phase 6.1] ✅ payout_method_availability table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_payout_history') THEN
        RAISE NOTICE '[Phase 6.1] ✅ agent_payout_history table created';
    END IF;
END $$;


