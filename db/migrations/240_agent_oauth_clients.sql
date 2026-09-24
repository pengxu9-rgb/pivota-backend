-- ADR-025 D1, MCP OAuth door: an OAuth client (a Claude / ChatGPT / Gemini connector) registered to
-- the agent its issued links are credited to (decision 2026-09-24). Unregistered clients stay
-- agent-less. One ACTIVE row per (issuer, client_id); re-pointing disables the old row.
--
-- Also declared in db/agent_oauth_clients.py; create_all creates it at startup (prod does not
-- apply numbered migrations).

CREATE TABLE IF NOT EXISTS agent_oauth_clients (
  id BIGSERIAL PRIMARY KEY,
  issuer VARCHAR(512) NOT NULL,
  client_id VARCHAR(512) NOT NULL,
  agent_id VARCHAR(64) NOT NULL,
  registered_by VARCHAR(128) NOT NULL,
  note VARCHAR(512),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  disabled_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_oauth_clients_active
  ON agent_oauth_clients (issuer, client_id)
  WHERE disabled_at IS NULL;
