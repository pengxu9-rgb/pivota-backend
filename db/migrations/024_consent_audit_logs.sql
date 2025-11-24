-- Migration 024: Consent Audit Logs
-- Date: 2025-11-09
-- Purpose: Add audit logging for consent management actions

-- Consent audit logs table
CREATE TABLE IF NOT EXISTS consent_audit_logs (
    log_id VARCHAR(100) PRIMARY KEY DEFAULT 'log_' || gen_random_uuid()::text,
    consent_id VARCHAR(128) REFERENCES agent_consents(consent_id) ON DELETE SET NULL,
    action VARCHAR(50) NOT NULL,
    admin_user VARCHAR(100) NOT NULL,
    agent_id VARCHAR(50),
    details JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_consent_audit_logs_consent ON consent_audit_logs(consent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_consent_audit_logs_agent ON consent_audit_logs(agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_consent_audit_logs_admin ON consent_audit_logs(admin_user, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_consent_audit_logs_action ON consent_audit_logs(action);

COMMENT ON TABLE consent_audit_logs IS 'Audit trail for all consent management actions';
COMMENT ON COLUMN consent_audit_logs.action IS 'Action type: issued, revoked, extended, bulk_revoked';
COMMENT ON COLUMN consent_audit_logs.admin_user IS 'Admin user who performed the action';
COMMENT ON COLUMN consent_audit_logs.details IS 'Additional action details (reason, scope, etc.)';

-- Verification
DO $$
BEGIN
    RAISE NOTICE '✅ Migration 024 completed - Consent audit logs ready';
END $$;

