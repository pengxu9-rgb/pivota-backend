-- ============================================================================
-- Migration 019: Agent Payouts System
-- ============================================================================
-- Purpose: Track and manage agent commission payouts
-- Created: 2024-11-07
-- Phase: 6 - Payouts & Banking
-- ============================================================================

-- ============================================================================
-- Part 1: agent_payouts - Core payout tracking table
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_payouts (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  agent_id VARCHAR(50) NOT NULL,
  amount NUMERIC(12,2) NOT NULL CHECK (amount >= 0),
  currency CHAR(3) NOT NULL DEFAULT 'USD',
  status VARCHAR(20) NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','uploaded','paid')),
  payout_reference VARCHAR(255),
  file_url TEXT,
  method VARCHAR(30),
  provider VARCHAR(50),
  external_id VARCHAR(100),
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  uploaded_at TIMESTAMPTZ,
  confirmed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_agent_payouts_merchant_status
  ON agent_payouts(merchant_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_payouts_agent
  ON agent_payouts(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_payouts_period
  ON agent_payouts(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_agent_payouts_created
  ON agent_payouts(created_at DESC);

-- ============================================================================
-- Part 2: agent_payout_links - Optional revenue reconciliation
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_payout_links (
  payout_id BIGINT NOT NULL REFERENCES agent_payouts(id) ON DELETE CASCADE,
  revenue_id BIGINT NOT NULL,
  amount NUMERIC(12,2) NOT NULL,
  PRIMARY KEY (payout_id, revenue_id)
);

CREATE INDEX IF NOT EXISTS idx_payout_links_revenue
  ON agent_payout_links(revenue_id);

-- ============================================================================
-- Part 3: Trigger for updated_at
-- ============================================================================

-- Create or replace the trigger function
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN 
  NEW.updated_at = NOW(); 
  RETURN NEW; 
END; 
$$ LANGUAGE plpgsql;

-- Drop and recreate trigger
DROP TRIGGER IF EXISTS trg_agent_payouts_updated_at ON agent_payouts;
CREATE TRIGGER trg_agent_payouts_updated_at
  BEFORE UPDATE ON agent_payouts
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

-- ============================================================================
-- Part 4: Comments for documentation
-- ============================================================================

COMMENT ON TABLE agent_payouts IS 'Phase 6: Tracks agent commission payouts from merchants';
COMMENT ON COLUMN agent_payouts.merchant_id IS 'Merchant who owes the commission';
COMMENT ON COLUMN agent_payouts.agent_id IS 'Agent receiving the payout';
COMMENT ON COLUMN agent_payouts.amount IS 'Payout amount in the specified currency';
COMMENT ON COLUMN agent_payouts.status IS 'pending: created, uploaded: proof uploaded, paid: confirmed payment';
COMMENT ON COLUMN agent_payouts.payout_reference IS 'External reference (e.g., wire transfer number)';
COMMENT ON COLUMN agent_payouts.file_url IS 'URL to payment proof document';
COMMENT ON COLUMN agent_payouts.method IS 'Payment method used (wire, ach, paypal, etc.)';
COMMENT ON COLUMN agent_payouts.provider IS 'Payment provider (bank name, paypal, etc.)';
COMMENT ON COLUMN agent_payouts.external_id IS 'External system transaction ID';
COMMENT ON COLUMN agent_payouts.period_start IS 'Start of commission period';
COMMENT ON COLUMN agent_payouts.period_end IS 'End of commission period';

COMMENT ON TABLE agent_payout_links IS 'Links payouts to specific revenue records for reconciliation';

-- ============================================================================
-- Part 5: Verification
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_payouts') THEN
        RAISE NOTICE '✅ Table agent_payouts created successfully';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_payout_links') THEN
        RAISE NOTICE '✅ Table agent_payout_links created successfully';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.triggers WHERE trigger_name = 'trg_agent_payouts_updated_at') THEN
        RAISE NOTICE '✅ Trigger trg_agent_payouts_updated_at created successfully';
    END IF;
    
    RAISE NOTICE '✅ Migration 019_agent_payouts completed successfully';
END $$;
