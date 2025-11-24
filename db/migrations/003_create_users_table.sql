-- Users table for authentication (employees, merchants, agents, merchants)
-- Cleaned-up version for PostgreSQL

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    role VARCHAR(50) NOT NULL CHECK (role IN ('super_admin', 'admin', 'employee', 'outsourced', 'merchant', 'agent')),
    merchant_id VARCHAR(50),
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login TIMESTAMP WITH TIME ZONE
);

-- Indexes for common lookups
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);

-- updated_at trigger
CREATE OR REPLACE FUNCTION update_users_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW
    EXECUTE FUNCTION update_users_updated_at();

-- Seed test users for all roles (password: Admin123!)
-- bcrypt hash for "Admin123!" reused from original migration
INSERT INTO users (email, password_hash, full_name, role, active)
VALUES 
    -- Employee roles (internal staff)
    ('superadmin@pivota.com', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aqfyH2T0vGEC', 'Super Admin', 'super_admin', TRUE),
    ('admin@pivota.com',      '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aqfyH2T0vGEC', 'Admin User',  'admin',       TRUE),
    ('employee@pivota.com',   '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aqfyH2T0vGEC', 'Employee User','employee',    TRUE),
    ('outsourced@pivota.com', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aqfyH2T0vGEC', 'Outsourced',   'outsourced',  TRUE),
    
    -- External roles
    ('merchant@test.com',     '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aqfyH2T0vGEC', 'Test Merchant','merchant',    TRUE),
    ('agent@test.com',        '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aqfyH2T0vGEC', 'Test Agent',   'agent',       TRUE)
ON CONFLICT (email) DO NOTHING;


















