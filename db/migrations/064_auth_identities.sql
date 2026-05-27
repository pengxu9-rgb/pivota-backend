-- Canonical identity + scoped portal memberships.
-- Additive migration: legacy users/employees/shop_users remain intact.

CREATE TABLE IF NOT EXISTS auth_identities (
  identity_id VARCHAR(64) PRIMARY KEY,
  email VARCHAR(255) NOT NULL,
  email_normalized VARCHAR(255) UNIQUE NOT NULL,
  full_name VARCHAR(255),
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_auth_identities_email_normalized
  ON auth_identities(email_normalized);

CREATE TABLE IF NOT EXISTS auth_credentials (
  identity_id VARCHAR(64) PRIMARY KEY REFERENCES auth_identities(identity_id) ON DELETE CASCADE,
  password_hash VARCHAR(255) NOT NULL,
  source VARCHAR(64),
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS auth_memberships (
  membership_id VARCHAR(64) PRIMARY KEY,
  identity_id VARCHAR(64) NOT NULL REFERENCES auth_identities(identity_id) ON DELETE CASCADE,
  membership_type VARCHAR(32) NOT NULL CHECK (membership_type IN ('employee', 'merchant', 'agent', 'customer')),
  role VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  entity_id VARCHAR(128) NOT NULL,
  permissions JSONB DEFAULT '[]'::jsonb,
  source VARCHAR(64),
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  CONSTRAINT uq_auth_memberships_identity_type_entity UNIQUE (identity_id, membership_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_auth_memberships_identity_type_status
  ON auth_memberships(identity_id, membership_type, status);

CREATE INDEX IF NOT EXISTS idx_auth_memberships_type_entity
  ON auth_memberships(membership_type, entity_id);

CREATE TABLE IF NOT EXISTS auth_identity_events (
  event_id VARCHAR(64) PRIMARY KEY,
  identity_id VARCHAR(64) REFERENCES auth_identities(identity_id) ON DELETE SET NULL,
  email_normalized VARCHAR(255),
  event_type VARCHAR(64) NOT NULL,
  details JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_auth_identity_events_identity_created
  ON auth_identity_events(identity_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_auth_identity_events_email_created
  ON auth_identity_events(email_normalized, created_at DESC);
