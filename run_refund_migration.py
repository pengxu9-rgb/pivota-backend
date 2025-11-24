#!/usr/bin/env python3
"""
Run refund tables migration
"""
import asyncio
import asyncpg

DATABASE_URL = "postgresql://postgres:dkDuBgsfeuvwxkiRssSEQsBjsvcAuMjN@metro.proxy.rlwy.net:19541/railway"

MIGRATION_SQL = """
-- Migration: Add refund tracking tables
-- Date: 2024-11-23
-- Purpose: Support refund management with idempotency and platform tracking

-- Add cumulative refund tracking to orders table
ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_refunded DECIMAL(10,2) DEFAULT 0;

-- Create refund_records table for detailed refund history
CREATE TABLE IF NOT EXISTS refund_records (
    refund_id VARCHAR(50) PRIMARY KEY,
    order_id VARCHAR(50) NOT NULL,
    merchant_id VARCHAR(50) NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD',
    reason VARCHAR(100),
    source VARCHAR(50), -- 'pivota_merchant'/'platform_webhook'/'admin'
    status VARCHAR(50) DEFAULT 'pending', -- 'pending'/'completed'/'failed'
    
    -- Platform tracking (for future phases)
    platform_type VARCHAR(50),
    platform_refund_id VARCHAR(255),
    platform_sync_status VARCHAR(50),
    
    -- PSP (Payment Service Provider) tracking
    psp_type VARCHAR(50),
    psp_refund_id VARCHAR(255),
    
    -- Metadata
    raw_payload JSONB,
    created_by VARCHAR(255),
    error_message TEXT,
    
    -- Idempotency
    idempotency_key VARCHAR(255) UNIQUE,
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    processed_at TIMESTAMP,
    
    -- Foreign key constraint
    CONSTRAINT fk_refund_order FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE RESTRICT
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_merchant_refunds ON refund_records (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_order_refunds ON refund_records (order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_idempotency ON refund_records (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_platform_refund ON refund_records (platform_type, platform_refund_id);

-- Create refund retry queue for failed refunds
CREATE TABLE IF NOT EXISTS refund_retry_queue (
    id SERIAL PRIMARY KEY,
    refund_id VARCHAR(50) NOT NULL,
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 3,
    next_retry_at TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Create index for retry queue processing
CREATE INDEX IF NOT EXISTS idx_retry_queue_next ON refund_retry_queue (next_retry_at) WHERE retry_count < max_retries;

-- Create a view for easy refund summary by order
CREATE OR REPLACE VIEW order_refund_summary AS
SELECT 
    o.order_id,
    o.merchant_id,
    o.total,
    o.total_refunded,
    COUNT(r.refund_id) as refund_count,
    COALESCE(SUM(r.amount) FILTER (WHERE r.status = 'completed'), 0) as confirmed_refunded,
    COALESCE(SUM(r.amount) FILTER (WHERE r.status = 'pending'), 0) as pending_refunds,
    (o.total - o.total_refunded) as refundable_amount
FROM orders o
LEFT JOIN refund_records r ON o.order_id = r.order_id
GROUP BY o.order_id, o.merchant_id, o.total, o.total_refunded;
"""

async def run_migration():
    print("🔧 Connecting to database...")
    conn = await asyncpg.connect(DATABASE_URL)
    
    try:
        print("📋 Running migration...")
        await conn.execute(MIGRATION_SQL)
        print("✅ Migration completed successfully!")
        
        # Verify tables were created
        print("\n🔍 Verifying tables...")
        tables = await conn.fetch("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            AND table_name LIKE 'refund%'
            ORDER BY table_name
        """)
        
        print("📊 Created tables:")
        for table in tables:
            print(f"  ✓ {table['table_name']}")
            
        # Check if total_refunded column was added
        columns = await conn.fetch("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'orders' 
            AND column_name = 'total_refunded'
        """)
        
        if columns:
            print(f"\n✅ orders.total_refunded column added: {columns[0]['data_type']}")
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        raise
    finally:
        await conn.close()
        print("\n🔌 Database connection closed")

if __name__ == "__main__":
    asyncio.run(run_migration())

