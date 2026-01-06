-- Checkout intents: short-lived checkout context used by shared checkout UI.
-- Stores optional shipping prefill (PII) server-side to avoid leaking into URLs.

CREATE TABLE IF NOT EXISTS checkout_intents (
  intent_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  buyer_ref TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  prefill JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_checkout_intents_agent_buyer
  ON checkout_intents(agent_id, buyer_ref);

CREATE INDEX IF NOT EXISTS idx_checkout_intents_expires_at
  ON checkout_intents(expires_at);

