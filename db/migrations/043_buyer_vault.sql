-- Migration 043: Buyer Vault (Unified Buyer Account)
-- Date: 2026-01-26
-- Purpose: Minimal schema for Pivota Unified Buyer Account (Stripe Link-like, no card storage)

-- ---------------------------------------------------------------------------
-- Accounts user upgrades (best-effort; safe if table does not exist yet)
-- ---------------------------------------------------------------------------

ALTER TABLE IF EXISTS shop_users
  ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------------
-- Buyer Vault: shipping addresses (no payment credentials)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS buyer_addresses (
  id TEXT PRIMARY KEY,
  buyer_id TEXT NOT NULL,
  recipient_name TEXT,
  line1 TEXT NOT NULL,
  line2 TEXT,
  city TEXT NOT NULL,
  region TEXT,
  postal_code TEXT NOT NULL,
  country TEXT NOT NULL,
  phone TEXT,
  is_default BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_buyer_addresses_buyer_created
  ON buyer_addresses(buyer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_buyer_addresses_buyer_default
  ON buyer_addresses(buyer_id, is_default);

-- ---------------------------------------------------------------------------
-- Pairwise buyer_ref: stable per (buyer_id, agent_id), opaque/non-enumerable
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS buyer_agent_links (
  id BIGSERIAL PRIMARY KEY,
  buyer_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  agent_scoped_buyer_ref TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_used_at TIMESTAMPTZ,
  UNIQUE (buyer_id, agent_id),
  UNIQUE (agent_id, agent_scoped_buyer_ref)
);

CREATE INDEX IF NOT EXISTS idx_buyer_agent_links_agent_last_used
  ON buyer_agent_links(agent_id, last_used_at DESC);

-- ---------------------------------------------------------------------------
-- Checkout intents extensions (MVP)
-- ---------------------------------------------------------------------------

ALTER TABLE IF EXISTS checkout_intents
  ADD COLUMN IF NOT EXISTS agent_user_ref TEXT,
  ADD COLUMN IF NOT EXISTS requested_scopes JSONB,
  ADD COLUMN IF NOT EXISTS linked_buyer_id TEXT,
  ADD COLUMN IF NOT EXISTS used_at TIMESTAMPTZ,
  -- Security hardening: bind prefill access to the originally issued checkout_token (store hash only),
  -- and enforce a small, configurable read budget.
  ADD COLUMN IF NOT EXISTS checkout_token_hash TEXT,
  ADD COLUMN IF NOT EXISTS prefill_read_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS prefill_last_read_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_checkout_intents_linked_buyer_created
  ON checkout_intents(linked_buyer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_checkout_intents_expires_used
  ON checkout_intents(expires_at, used_at);

-- ---------------------------------------------------------------------------
-- Orders extensions (internal linkage only; never exposed to agent)
-- ---------------------------------------------------------------------------

ALTER TABLE IF EXISTS orders
  ADD COLUMN IF NOT EXISTS buyer_id TEXT,
  ADD COLUMN IF NOT EXISTS intent_id TEXT,
  ADD COLUMN IF NOT EXISTS agent_user_ref TEXT,
  ADD COLUMN IF NOT EXISTS agent_scoped_buyer_ref TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_buyer_created
  ON orders(buyer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orders_agent_scoped_buyer_ref
  ON orders(agent_id, agent_scoped_buyer_ref);

-- ---------------------------------------------------------------------------
-- Mandates (future groundwork; no PSP integration yet)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mandates (
  id TEXT PRIMARY KEY,
  buyer_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL,
  constraints_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mandates_buyer_created
  ON mandates(buyer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mandates_agent_created
  ON mandates(agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mandates_status_expires
  ON mandates(status, expires_at);

-- ---------------------------------------------------------------------------
-- Authorization tokens (constrained capability; store hash only)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS authorization_tokens (
  id TEXT PRIMARY KEY,
  mandate_id TEXT NOT NULL,
  intent_id TEXT,
  token_hash TEXT NOT NULL,
  scope_json JSONB,
  amount NUMERIC(12,2),
  currency TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  used_at TIMESTAMPTZ,
  UNIQUE (token_hash)
);

CREATE INDEX IF NOT EXISTS idx_authorization_tokens_mandate_created
  ON authorization_tokens(mandate_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_authorization_tokens_expires_used
  ON authorization_tokens(expires_at, used_at);

-- ---------------------------------------------------------------------------
-- Buyer audit log (PII-minimized JSONB details)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS buyer_audit_logs (
  id BIGSERIAL PRIMARY KEY,
  buyer_id TEXT,
  agent_id TEXT,
  action TEXT NOT NULL,
  details JSONB,
  ip_address TEXT,
  user_agent TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_buyer_audit_logs_buyer_created
  ON buyer_audit_logs(buyer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_buyer_audit_logs_agent_created
  ON buyer_audit_logs(agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_buyer_audit_logs_action_created
  ON buyer_audit_logs(action, created_at DESC);

-- ---------------------------------------------------------------------------
-- Save-for-next-time binding (step-up challenge)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS buyer_save_challenges (
  save_token_hash TEXT PRIMARY KEY,
  intent_id TEXT NOT NULL,
  order_id TEXT,
  checkout_token_hash TEXT,
  client_nonce_hash TEXT NOT NULL,
  save_email BOOLEAN NOT NULL DEFAULT TRUE,
  save_address BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  redeemed_at TIMESTAMPTZ,
  redeemed_buyer_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_buyer_save_challenges_intent_created
  ON buyer_save_challenges(intent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_buyer_save_challenges_expires_redeemed
  ON buyer_save_challenges(expires_at, redeemed_at);

DO $$
BEGIN
  RAISE NOTICE '✅ Migration 043 completed - Buyer Vault schema ready';
END $$;
