-- Lifecycle for browser collector tokens (universal web collector, Shopify
-- web pixel).
--
-- Until now issuing a token meant signing a JWT and handing it over. Nothing
-- recorded that it existed, so there was no way to list a store's tokens, no
-- way to revoke one short of disconnecting the store or rotating the signing
-- secret for every merchant at once, no persisted expiry to alert on, and the
-- TTL cap was 400 days.
--
-- merchant_collector_tokens is one row per issued token, keyed by the JWT's
-- `jti`. merchant_collector_token_policy holds a per-store generation: a
-- token whose `sv` claim is below the store's min_token_version is refused
-- whether or not its row exists, which is how tokens issued before this
-- registry (format v1, no jti) are revoked as a set.
--
-- The SQLAlchemy model in db/merchant_collector_tokens.py builds a fresh
-- database; this file brings an existing one to the same shape.

CREATE TABLE IF NOT EXISTS merchant_collector_tokens (
    jti VARCHAR(64) PRIMARY KEY,
    merchant_id VARCHAR(50) NOT NULL,
    store_id VARCHAR(128) NOT NULL,
    token_type VARCHAR(32) NOT NULL,
    token_version INTEGER NOT NULL,
    store_token_version INTEGER NOT NULL,
    allowed_origins JSONB NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ NULL,
    revoked_reason VARCHAR(64) NULL,
    superseded_by VARCHAR(64) NULL,
    issued_by VARCHAR(128) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_merchant_collector_tokens_store
  ON merchant_collector_tokens (merchant_id, store_id);

CREATE INDEX IF NOT EXISTS idx_merchant_collector_tokens_expiring
  ON merchant_collector_tokens (expires_at)
  WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS merchant_collector_token_policy (
    store_id VARCHAR(128) PRIMARY KEY,
    merchant_id VARCHAR(50) NOT NULL,
    min_token_version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
