-- Buyer ref aliases: allow guest buyer_ref to be merged into a canonical user buyer_ref (agent-scoped).
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS buyer_ref_aliases (
  id          BIGSERIAL PRIMARY KEY,
  agent_id    VARCHAR(100) NOT NULL,
  source_ref  TEXT NOT NULL,
  target_ref  TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (agent_id, source_ref)
);

CREATE INDEX IF NOT EXISTS idx_buyer_ref_aliases_agent_target
  ON buyer_ref_aliases(agent_id, target_ref);

