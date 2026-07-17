-- Migration 183: agents.did — AP2 self-sovereign DID identity (ADR-012)
-- Date: 2026-07-17
-- Purpose: Add the durable home for an agent's DID (did:key / did:web), used as
-- its AP2 external identity on the signed rail.
--
-- ADR-012 "DID ↔ agent association" decision: the DID and the internal agent_id
-- are MAPPED, not merged. agent_id stays the internal identifier (and the target
-- of every existing FK: agent_consents.agent_id, agent_wallets, nonce_tracker,
-- x402_transactions); this column maps a DID to that row. On the AP2 rail Pivota
-- resolves the presented DID -> verification key (services/ap2_identity.py) AND
-- DID -> agent row via this column (for x402_enabled, wallet binding, limits).
--
-- Distinct from agents.public_key (migration 021): public_key is a raw-PEM pilot
-- fallback; a DID agent carries its key IN the DID, not here.
--
-- Additive + nullable: only DID-enabled agents populate it. The partial unique
-- index enforces "one DID -> at most one agent" while allowing many NULLs.
-- Idempotent so it is safe regardless of current live state.

-- schema-guard-exempt: agents.did is an opt-in AP2 column. The AP2 router is
-- disabled in every environment (ENABLE_AP2_ROUTES=false); the whole AP2 schema
-- (021 + 182 + this) is applied on demand via the admin migration runner before
-- AP2 is enabled, so this column needs no boot-time self-heal.

ALTER TABLE IF EXISTS agents
  ADD COLUMN IF NOT EXISTS did TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_did_unique
  ON agents (did) WHERE did IS NOT NULL;

COMMENT ON COLUMN agents.did IS
  'AP2 self-sovereign DID (did:key/did:web) — external identity for the signed rail (ADR-012). agent_id stays the internal id; this maps a DID to the agent.';

DO $$
BEGIN
    RAISE NOTICE '✅ Migration 183 completed - agents.did present (unique when set)';
END $$;
