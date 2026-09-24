-- ADR-025 D1, MCP OAuth door: an OAuth client REDIRECT ORIGIN (e.g. https://claude.ai) registered to
-- the agent whose links a connector completing grants there is credited to (decision 2026-09-24).
-- Keyed by origin, not client_id: open dynamic registration mints a random client_id per install,
-- while the redirect origin is what the authorization server verifies. Unregistered origins stay
-- agent-less. One ACTIVE row per (issuer, redirect_origin); re-pointing disables the old row.
--
-- Also declared in db/agent_oauth_client_origins.py; create_all creates it at startup (prod does not
-- apply numbered migrations).

CREATE TABLE IF NOT EXISTS agent_oauth_client_origins (
  id BIGSERIAL PRIMARY KEY,
  issuer VARCHAR(512) NOT NULL,
  redirect_origin VARCHAR(512) NOT NULL,
  agent_id VARCHAR(64) NOT NULL,
  registered_by VARCHAR(128) NOT NULL,
  note VARCHAR(512),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  disabled_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_oauth_client_origins_active
  ON agent_oauth_client_origins (issuer, redirect_origin)
  WHERE disabled_at IS NULL;
