-- Migration 029: Platform Orders (side-car cache for imported platform orders)
-- Purpose: store normalized platform orders ingested from CSV/API (Amazon/Temu)

CREATE TABLE IF NOT EXISTS platform_orders (
    id SERIAL PRIMARY KEY,
    merchant_id VARCHAR(50) NOT NULL,
    platform VARCHAR(50) NOT NULL,
    order_id VARCHAR(100) NOT NULL,
    order_item_id VARCHAR(100),
    data JSONB NOT NULL,
    import_task_id INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Basic uniqueness to avoid duplicate rows for the same platform order item
CREATE UNIQUE INDEX IF NOT EXISTS ux_platform_orders_unique_order
    ON platform_orders (merchant_id, platform, order_id, COALESCE(order_item_id, ''));

CREATE INDEX IF NOT EXISTS idx_platform_orders_task
    ON platform_orders (import_task_id);

CREATE INDEX IF NOT EXISTS idx_platform_orders_merchant_platform
    ON platform_orders (merchant_id, platform, created_at DESC);

