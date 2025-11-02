-- Migration 009: Agent Observability & Governance - Phase 3
-- Date: 2025-11-02
-- Purpose: Add metrics collection, anomaly detection, and governance automation

-- ============================================================================
-- Part 1: agent_metrics - Time-series performance data
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_metrics (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    avg_response_time_ms INTEGER DEFAULT 0,
    success_rate NUMERIC(5, 2) DEFAULT 0,
    error_rate NUMERIC(5, 2) DEFAULT 0,
    queries_per_min INTEGER DEFAULT 0,
    total_queries_count INTEGER DEFAULT 0,
    period_minutes INTEGER DEFAULT 5,
    last_seen_at TIMESTAMP WITH TIME ZONE,
    collected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_metrics_agent_ts ON agent_metrics(agent_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_metrics_last_seen ON agent_metrics(agent_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_metrics_collected ON agent_metrics(collected_at DESC);

COMMENT ON TABLE agent_metrics IS 'Time-series performance metrics collected every 5 minutes';
COMMENT ON COLUMN agent_metrics.avg_response_time_ms IS 'Average API response time in milliseconds';
COMMENT ON COLUMN agent_metrics.success_rate IS 'Percentage of successful calls (0-100)';
COMMENT ON COLUMN agent_metrics.error_rate IS 'Percentage of failed calls (0-100)';
COMMENT ON COLUMN agent_metrics.queries_per_min IS 'Queries per minute in this period';
COMMENT ON COLUMN agent_metrics.last_seen_at IS 'Last activity timestamp for staleness detection';

-- ============================================================================
-- Part 2: agent_alerts - Governance alerts
-- ============================================================================

CREATE TABLE IF NOT EXISTS agent_alerts (
    id SERIAL PRIMARY KEY,
    alert_id VARCHAR(50) UNIQUE NOT NULL,
    agent_id VARCHAR(50) NOT NULL,
    alert_type VARCHAR(50) NOT NULL,
    severity VARCHAR(20) NOT NULL,
    message TEXT NOT NULL,
    metadata JSON,
    resolved BOOLEAN DEFAULT false,
    resolved_at TIMESTAMP WITH TIME ZONE,
    resolved_by VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_alerts_agent ON agent_alerts(agent_id, resolved, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_alerts_severity ON agent_alerts(severity, resolved);
CREATE INDEX IF NOT EXISTS idx_agent_alerts_type ON agent_alerts(alert_type, resolved);

COMMENT ON TABLE agent_alerts IS 'Governance alerts for anomalous agent behavior';
COMMENT ON COLUMN agent_alerts.alert_type IS 'high_error_rate, high_latency, rate_limit_exceeded, unusual_spike';
COMMENT ON COLUMN agent_alerts.severity IS 'info, warning, critical';

-- ============================================================================
-- Part 3: governance_actions_log - Audit trail for governance actions
-- ============================================================================

CREATE TABLE IF NOT EXISTS governance_actions_log (
    id SERIAL PRIMARY KEY,
    action_id VARCHAR(50) UNIQUE NOT NULL,
    agent_id VARCHAR(50) NOT NULL,
    action_type VARCHAR(50) NOT NULL,
    triggered_by VARCHAR(20) NOT NULL,
    executed_by VARCHAR(100),
    action_payload JSON,
    status VARCHAR(20) DEFAULT 'pending',
    reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    executed_at TIMESTAMP WITH TIME ZONE,
    
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gov_actions_agent ON governance_actions_log(agent_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gov_actions_status ON governance_actions_log(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gov_actions_type ON governance_actions_log(action_type, status);

COMMENT ON TABLE governance_actions_log IS 'Audit log for all governance actions (pending, approved, rejected, executed)';
COMMENT ON COLUMN governance_actions_log.triggered_by IS 'auto or manual';
COMMENT ON COLUMN governance_actions_log.status IS 'pending, approved, rejected, executed, failed';
COMMENT ON COLUMN governance_actions_log.action_type IS 'reduce_rate_limit, suspend_agent, require_key_rotation, data_quality_warning';

-- ============================================================================
-- Migration complete
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_metrics') THEN
        RAISE NOTICE '✅ agent_metrics table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'agent_alerts') THEN
        RAISE NOTICE '✅ agent_alerts table created';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'governance_actions_log') THEN
        RAISE NOTICE '✅ governance_actions_log table created';
    END IF;
END $$;

