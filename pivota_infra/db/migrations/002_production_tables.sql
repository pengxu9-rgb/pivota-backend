-- Production Database Schema Updates
-- Run this after initial setup to add production-ready tables

-- Agent API usage tracking
CREATE TABLE IF NOT EXISTS agent_api_logs (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    endpoint VARCHAR(500) NOT NULL,
    method VARCHAR(10) NOT NULL,
    request_body TEXT,
    response_status INTEGER NOT NULL,
    response_time_ms INTEGER NOT NULL,
    error_message TEXT,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_agent_id (agent_id),
    INDEX idx_created_at (created_at),
    INDEX idx_endpoint (endpoint)
);

-- Aggregated agent metrics (for performance)
CREATE TABLE IF NOT EXISTS agent_metrics (
    agent_id VARCHAR(255) PRIMARY KEY,
    total_api_calls INTEGER DEFAULT 0,
    total_orders_initiated INTEGER DEFAULT 0,
    total_orders_completed INTEGER DEFAULT 0,
    total_gmv DECIMAL(15,2) DEFAULT 0.00,
    avg_response_time_ms INTEGER DEFAULT 0,
    last_activity_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- PSP performance tracking
CREATE TABLE IF NOT EXISTS psp_attempts (
    id SERIAL PRIMARY KEY,
    merchant_id VARCHAR(255) NOT NULL,
    order_id VARCHAR(255),
    psp_type VARCHAR(50) NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    success BOOLEAN NOT NULL,
    response_time_ms INTEGER,
    error_code VARCHAR(100),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_merchant_psp (merchant_id, psp_type),
    INDEX idx_order_id (order_id),
    INDEX idx_created_at (created_at)
);

-- MCP query logs
CREATE TABLE IF NOT EXISTS mcp_query_logs (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    tool_name VARCHAR(100) NOT NULL,  -- e.g., 'catalog.search', 'inventory.check'
    query_params TEXT,
    response_status VARCHAR(50),
    response_time_ms INTEGER,
    result_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_agent_tool (agent_id, tool_name),
    INDEX idx_created_at (created_at)
);

-- Rate limiting tracking
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    bucket_key VARCHAR(255) PRIMARY KEY,  -- e.g., 'agent:123:minute', 'agent:123:hour'
    request_count INTEGER DEFAULT 0,
    window_start TIMESTAMP NOT NULL,
    window_end TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_window_end (window_end)
);

-- Webhook event logs
CREATE TABLE IF NOT EXISTS webhook_events (
    id SERIAL PRIMARY KEY,
    merchant_id VARCHAR(255),
    event_type VARCHAR(100) NOT NULL,
    source VARCHAR(50) NOT NULL,  -- 'stripe', 'shopify', etc.
    payload TEXT NOT NULL,
    status VARCHAR(50) DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_attempt_at TIMESTAMP,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    INDEX idx_merchant_event (merchant_id, event_type),
    INDEX idx_status (status),
    INDEX idx_created_at (created_at)
);

-- Session management for agents
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id VARCHAR(255) PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    api_key_hash VARCHAR(255) NOT NULL,
    ip_address VARCHAR(45),
    user_agent TEXT,
    last_activity_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_agent_id (agent_id),
    INDEX idx_expires_at (expires_at)
);

-- Audit log for sensitive operations
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,  -- 'merchant', 'agent', 'admin'
    entity_id VARCHAR(255) NOT NULL,
    action VARCHAR(100) NOT NULL,
    details TEXT,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_entity (entity_type, entity_id),
    INDEX idx_action (action),
    INDEX idx_created_at (created_at)
);

-- Error tracking
CREATE TABLE IF NOT EXISTS error_logs (
    id SERIAL PRIMARY KEY,
    error_code VARCHAR(100),
    error_type VARCHAR(100) NOT NULL,
    error_message TEXT NOT NULL,
    stack_trace TEXT,
    context TEXT,  -- JSON with request details
    severity VARCHAR(20) DEFAULT 'error',  -- 'warning', 'error', 'critical'
    resolved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    INDEX idx_error_type (error_type),
    INDEX idx_severity (severity),
    INDEX idx_created_at (created_at),
    INDEX idx_resolved (resolved)
);

-- Platform analytics (for business intelligence)
CREATE TABLE IF NOT EXISTS platform_analytics (
    id SERIAL PRIMARY KEY,
    metric_date DATE NOT NULL,
    metric_type VARCHAR(100) NOT NULL,
    metric_value DECIMAL(15,2),
    dimensions TEXT,  -- JSON with breakdown dimensions
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY idx_metric_date_type (metric_date, metric_type)
);

-- Add indexes to existing tables for better performance
ALTER TABLE orders ADD INDEX IF NOT EXISTS idx_created_at (created_at);
ALTER TABLE orders ADD INDEX IF NOT EXISTS idx_status (status);
ALTER TABLE orders ADD INDEX IF NOT EXISTS idx_merchant_id (merchant_id);
ALTER TABLE orders ADD INDEX IF NOT EXISTS idx_agent_id (agent_id);

ALTER TABLE merchant_onboarding ADD INDEX IF NOT EXISTS idx_status (status);
ALTER TABLE merchant_onboarding ADD INDEX IF NOT EXISTS idx_created_at (created_at);

ALTER TABLE agent_onboarding ADD INDEX IF NOT EXISTS idx_status (status);
ALTER TABLE agent_onboarding ADD INDEX IF NOT EXISTS idx_api_key (api_key);

-- Create views for common queries
CREATE OR REPLACE VIEW agent_performance_summary AS
SELECT 
    a.agent_id,
    ao.name as agent_name,
    a.total_api_calls,
    a.total_orders_initiated,
    a.total_orders_completed,
    CASE 
        WHEN a.total_orders_initiated > 0 
        THEN ROUND(a.total_orders_completed * 100.0 / a.total_orders_initiated, 2)
        ELSE 0 
    END as conversion_rate,
    a.total_gmv,
    CASE 
        WHEN a.total_orders_completed > 0 
        THEN ROUND(a.total_gmv / a.total_orders_completed, 2)
        ELSE 0 
    END as avg_order_value,
    a.avg_response_time_ms,
    a.last_activity_at
FROM agent_metrics a
LEFT JOIN agent_onboarding ao ON a.agent_id = ao.agent_id
ORDER BY a.total_gmv DESC;

CREATE OR REPLACE VIEW daily_platform_metrics AS
SELECT 
    DATE(created_at) as date,
    COUNT(DISTINCT agent_id) as active_agents,
    COUNT(DISTINCT merchant_id) as active_merchants,
    COUNT(*) as total_orders,
    COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed_orders,
    SUM(CASE WHEN status = 'completed' THEN total_amount ELSE 0 END) as total_gmv,
    AVG(CASE WHEN status = 'completed' THEN total_amount ELSE NULL END) as avg_order_value
FROM orders
GROUP BY DATE(created_at)
ORDER BY date DESC;

