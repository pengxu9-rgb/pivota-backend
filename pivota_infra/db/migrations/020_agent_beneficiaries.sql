-- ============================================================================
-- Migration 020: Agent Beneficiaries (Bank Account Information)
-- ============================================================================
-- Purpose: Store agent bank account details for payouts
-- Created: 2024-11-07
-- Phase: 6 - Payouts & Banking
-- ============================================================================

-- ============================================================================
-- Part 1: agent_beneficiaries - Bank account information
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_beneficiaries (
  id BIGSERIAL PRIMARY KEY,
  agent_id VARCHAR(50) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  method VARCHAR(30) NOT NULL DEFAULT 'bank_wire',
  currency CHAR(3) NOT NULL DEFAULT 'USD',
  
  -- Account holder information
  account_holder_name VARCHAR(255),
  
  -- International (SWIFT/IBAN)
  iban VARCHAR(34),
  swift_bic VARCHAR(11),
  bank_name VARCHAR(255),
  bank_country CHAR(2),
  
  -- US Domestic (ACH)
  account_number VARCHAR(34),
  routing_number VARCHAR(20),
  
  -- Display/Security fields
  account_number_last4 CHAR(4),
  iban_preview VARCHAR(20), -- e.g., "DE89...1234"
  
  -- Permissions and verification
  allow_share_with_merchants BOOLEAN DEFAULT FALSE,
  verify_status VARCHAR(20) DEFAULT 'unverified'
    CHECK (verify_status IN ('unverified', 'pending', 'verified', 'failed')),
  verified_at TIMESTAMPTZ,
  
  -- Metadata and timestamps
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  
  -- Ensure one beneficiary per agent/method/currency combination
  UNIQUE (agent_id, method, currency)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_bene_agent ON agent_beneficiaries(agent_id);
CREATE INDEX IF NOT EXISTS idx_bene_share ON agent_beneficiaries(allow_share_with_merchants) 
  WHERE allow_share_with_merchants = TRUE;
CREATE INDEX IF NOT EXISTS idx_bene_verify ON agent_beneficiaries(verify_status);
CREATE INDEX IF NOT EXISTS idx_bene_method ON agent_beneficiaries(method);

-- ============================================================================
-- Part 2: Trigger for updated_at
-- ============================================================================

-- Reuse the function from previous migration if exists, or create it
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN 
  NEW.updated_at = NOW(); 
  RETURN NEW; 
END; 
$$ LANGUAGE plpgsql;

-- Create trigger
DROP TRIGGER IF EXISTS trg_bene_updated_at ON agent_beneficiaries;
CREATE TRIGGER trg_bene_updated_at
  BEFORE UPDATE ON agent_beneficiaries
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

-- ============================================================================
-- Part 3: Comments for documentation
-- ============================================================================

COMMENT ON TABLE agent_beneficiaries IS 'Phase 6: Agent bank account information for receiving payouts';
COMMENT ON COLUMN agent_beneficiaries.agent_id IS 'Agent who owns this bank account';
COMMENT ON COLUMN agent_beneficiaries.method IS 'Payout method: bank_wire, ach, sepa, paypal, etc.';
COMMENT ON COLUMN agent_beneficiaries.currency IS 'Currency for this account (ISO 4217)';
COMMENT ON COLUMN agent_beneficiaries.account_holder_name IS 'Name on the bank account';
COMMENT ON COLUMN agent_beneficiaries.iban IS 'International Bank Account Number';
COMMENT ON COLUMN agent_beneficiaries.swift_bic IS 'SWIFT/BIC code for international transfers';
COMMENT ON COLUMN agent_beneficiaries.bank_name IS 'Name of the bank';
COMMENT ON COLUMN agent_beneficiaries.bank_country IS 'ISO 3166-1 alpha-2 country code';
COMMENT ON COLUMN agent_beneficiaries.account_number IS 'Domestic account number';
COMMENT ON COLUMN agent_beneficiaries.routing_number IS 'US routing number or equivalent';
COMMENT ON COLUMN agent_beneficiaries.account_number_last4 IS 'Last 4 digits for display';
COMMENT ON COLUMN agent_beneficiaries.iban_preview IS 'Masked IBAN for display (first 4 + last 4)';
COMMENT ON COLUMN agent_beneficiaries.allow_share_with_merchants IS 'Whether merchants can see bank details';
COMMENT ON COLUMN agent_beneficiaries.verify_status IS 'Bank account verification status';
COMMENT ON COLUMN agent_beneficiaries.verified_at IS 'When the account was verified';

-- ============================================================================
-- Part 4: Sample data for testing (commented out)
-- ============================================================================

/*
-- Example: US bank account
INSERT INTO agent_beneficiaries (
  agent_id, method, currency, account_holder_name,
  account_number, routing_number, account_number_last4,
  bank_name, bank_country
) VALUES (
  'agent@test.com', 'ach', 'USD', 'Test Agent LLC',
  '123456789', '021000021', '6789',
  'JPMorgan Chase', 'US'
);

-- Example: European IBAN account
INSERT INTO agent_beneficiaries (
  agent_id, method, currency, account_holder_name,
  iban, swift_bic, iban_preview,
  bank_name, bank_country
) VALUES (
  'agent@test.com', 'sepa', 'EUR', 'Test Agent GmbH',
  'DE89370400440532013000', 'DEUTDEFF', 'DE89...3000',
  'Deutsche Bank', 'DE'
);
*/

-- ============================================================================
-- Part 5: Verification
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_beneficiaries') THEN
        RAISE NOTICE '✅ Table agent_beneficiaries created successfully';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.triggers WHERE trigger_name = 'trg_bene_updated_at') THEN
        RAISE NOTICE '✅ Trigger trg_bene_updated_at created successfully';
    END IF;
    
    -- Check foreign key constraint
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints 
        WHERE constraint_type = 'FOREIGN KEY' 
        AND table_name = 'agent_beneficiaries'
        AND constraint_name LIKE '%agent_id%'
    ) THEN
        RAISE NOTICE '✅ Foreign key to agents table created successfully';
    END IF;
    
    RAISE NOTICE '✅ Migration 020_agent_beneficiaries completed successfully';
END $$;
