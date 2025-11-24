-- Migration 030: Amazon Feeds Tracking
-- Purpose: Track Amazon SP-API Feed submissions for order fulfillment and other updates

CREATE TABLE IF NOT EXISTS amazon_feeds (
    id SERIAL PRIMARY KEY,
    merchant_id VARCHAR(50) NOT NULL,
    feed_type VARCHAR(100) NOT NULL, -- e.g., 'POST_ORDER_FULFILLMENT_DATA'
    feed_id VARCHAR(100) UNIQUE, -- Amazon's feedId
    feed_document_id VARCHAR(100), -- Amazon's feedDocumentId
    
    -- Feed submission details
    submission_data JSONB NOT NULL, -- Original submission payload
    submission_result JSONB, -- Feed processing result from Amazon
    
    -- Status tracking
    status VARCHAR(50) NOT NULL DEFAULT 'pending', -- pending, submitted, in_progress, done, cancelled, failed
    processing_status VARCHAR(50), -- Amazon's processingStatus
    
    -- Related entities
    related_orders TEXT[], -- Array of order IDs affected by this feed
    
    -- Timestamps
    submitted_at TIMESTAMP WITH TIME ZONE,
    started_processing_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_amazon_feeds_merchant
    ON amazon_feeds (merchant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_amazon_feeds_status
    ON amazon_feeds (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_amazon_feeds_type
    ON amazon_feeds (feed_type, status);

-- Index for finding feeds by related orders
CREATE INDEX IF NOT EXISTS idx_amazon_feeds_orders
    ON amazon_feeds USING GIN (related_orders);

-- Add shipment tracking fields to platform_orders
ALTER TABLE platform_orders 
ADD COLUMN IF NOT EXISTS fulfillment_status VARCHAR(50) DEFAULT 'unfulfilled',
ADD COLUMN IF NOT EXISTS shipment_data JSONB,
ADD COLUMN IF NOT EXISTS fulfilled_at TIMESTAMP WITH TIME ZONE;

-- Index for fulfillment queries
CREATE INDEX IF NOT EXISTS idx_platform_orders_fulfillment
    ON platform_orders (merchant_id, platform, fulfillment_status)
    WHERE platform = 'amazon';
