-- Migration 024: Webhook Events Table for Idempotency and Auditing
-- Created: 2025-11-10
-- Purpose: Track all incoming webhook events for idempotency, auditing, and debugging

-- Create webhook_events table
CREATE TABLE IF NOT EXISTS webhook_events (
    id SERIAL PRIMARY KEY,
    
    -- Event identification
    event_id VARCHAR(255) UNIQUE NOT NULL,  -- External event ID from PSP (e.g., chk_evt_xxx, evt_xxx)
    event_type VARCHAR(100) NOT NULL,        -- payment_captured, payment_approved, etc.
    psp_type VARCHAR(50) NOT NULL,           -- checkout, stripe, adyen, etc.
    
    -- Order linkage
    order_id VARCHAR(50),                     -- Reference to orders table
    reference VARCHAR(255),                   -- Additional reference field
    
    -- Payload and headers
    payload JSONB NOT NULL,                   -- Full webhook payload
    headers JSONB,                            -- Request headers (for signature verification)
    
    -- Processing status
    -- 'unmatched' is written by the Stripe handler for a signed event it
    -- REFUSED to apply (cross-tenant block, or an amount that does not match the
    -- order). It is terminal and has NO automated consumer — see the column
    -- comment below. Only 'processed' and 'ignored' count as duplicates in
    -- WebhookService.check_duplicate_event, so an 'unmatched' row reprocesses if
    -- the event is redelivered.
    status VARCHAR(50) NOT NULL DEFAULT 'pending',  -- pending, processed, failed, ignored, duplicate, unmatched
    processed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    
    -- Signature verification
    signature_verified BOOLEAN DEFAULT FALSE,
    signature_header VARCHAR(500),
    
    -- Retry tracking
    retry_count INTEGER DEFAULT 0,
    last_retry_at TIMESTAMP WITH TIME ZONE,
    
    -- Timestamps
    received_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_webhook_events_event_id ON webhook_events(event_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_order_id ON webhook_events(order_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_status ON webhook_events(status);
CREATE INDEX IF NOT EXISTS idx_webhook_events_psp_type ON webhook_events(psp_type);
CREATE INDEX IF NOT EXISTS idx_webhook_events_event_type ON webhook_events(event_type);
CREATE INDEX IF NOT EXISTS idx_webhook_events_received_at ON webhook_events(received_at DESC);

-- Composite index for idempotency checks
CREATE INDEX IF NOT EXISTS idx_webhook_events_idempotency 
ON webhook_events(event_id, order_id, status);

-- Create updated_at trigger
CREATE OR REPLACE FUNCTION update_webhook_events_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_webhook_events_updated_at
    BEFORE UPDATE ON webhook_events
    FOR EACH ROW
    EXECUTE FUNCTION update_webhook_events_updated_at();

-- Add comment
COMMENT ON TABLE webhook_events IS 'Stores all incoming webhook events for idempotency, auditing, and debugging';
COMMENT ON COLUMN webhook_events.event_id IS 'Unique external event ID from PSP';
COMMENT ON COLUMN webhook_events.status IS 'Processing status: pending, processed, failed, ignored, duplicate, unmatched. "unmatched" = a signed event we permanently refused to apply (cross-tenant block or amount mismatch); it is terminal and nothing sweeps it, so it needs human follow-up from the accompanying alert. A possibly-transient refusal is NOT recorded here as terminal: the handler answers 503 so Stripe redelivers, and the row lands as "failed" until a redelivery succeeds.';
COMMENT ON COLUMN webhook_events.signature_verified IS 'Whether the webhook signature was verified';

