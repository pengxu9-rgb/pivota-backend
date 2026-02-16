-- Migration 044: agent user identity links (agent-scoped -> buyer_id)
-- Purpose:
-- - map verified agent_user_ref (stored as hash) to Pivota buyer_id
-- - enable cross-checkout prefill when the external agent has its own account system
-- - keep mapping agent-scoped and avoid storing raw agent_user_ref

CREATE TABLE IF NOT EXISTS buyer_identity_links (
  id BIGSERIAL PRIMARY KEY,
  agent_id TEXT NOT NULL,
  agent_user_ref_hash TEXT NOT NULL,
  buyer_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ,
  UNIQUE (agent_id, agent_user_ref_hash)
);

CREATE INDEX IF NOT EXISTS idx_buyer_identity_links_buyer_created
  ON buyer_identity_links(buyer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_buyer_identity_links_agent_buyer
  ON buyer_identity_links(agent_id, buyer_id);

