-- 193: agent_identity_issuers — FEDERATED buyer identity, per agent.
--
-- WHY THIS EXISTS. A buyer agent (Minds) owns its users. Today the only way its user can be
-- identified on a checkout call is (a) Pivota's own OAuth consent flow — the user leaves the
-- agent's UI to sign in with Pivota — or (b) an `X-Agent-User-JWT` verified against ONE issuer
-- configured by Pivota ops in env (AGENT_USER_JWT_ISSUERS / IDENTITY_ISSUERS_JSON on the
-- gateway). Neither scales to N agents: (a) is UX friction the agent did not choose, (b) is an
-- ops ticket per partner and binds nothing — any configured issuer's tokens are accepted from
-- any agent key.
--
-- This table lets an agent register ITS OWN token issuer from the developer portal, and binds
-- that issuer to that agent: a token minted by the agent's issuer is accepted only when it is
-- presented with that agent's API key. The gateway pulls the active rows (internal endpoint,
-- X-Internal-Key) into its per-issuer verifier registry; the REST verifier reads them directly.
--
--   agent_id          the owning agent (agents.agent_id). One agent may register several
--                     issuers (staging + prod); one issuer string belongs to ONE active agent
--                     (partial unique index below) so the binding is never ambiguous.
--   issuer            the exact `iss` claim value. Must not contain '|' (the gateway derives
--                     user_ref from `${iss}|${sub}`).
--   jwks_uri          https URL of the issuer's JWKS. Dereferenced at registration; must parse
--                     as { keys: [...] } with at least one RSA/EC/OKP key. Token-embedded jku /
--                     x5u are never honoured; only this pinned URL.
--   audience          the `aud` the agent's tokens carry for Pivota. Required.
--   algs              explicit asymmetric allowlist (RS*/PS*/ES*/EdDSA). Never HS*, never none.
--   authorized_party  optional: required `azp` / `client_id` claim value.
--   required_scopes   optional: every listed scope must be present in `scope` / `scp`.
--   status            active | disabled. Disabled rows are kept for audit; the unique index
--                     ignores them so the issuer can be re-registered.
--   last_jwks_ok_at   when the JWKS last dereferenced successfully (registration + re-checks).

CREATE TABLE IF NOT EXISTS agent_identity_issuers (
    id               BIGSERIAL PRIMARY KEY,
    agent_id         TEXT        NOT NULL,
    issuer           TEXT        NOT NULL,
    jwks_uri         TEXT        NOT NULL,
    audience         TEXT        NOT NULL,
    algs             TEXT[]      NOT NULL DEFAULT ARRAY['RS256', 'ES256']::TEXT[],
    authorized_party TEXT        NULL,
    required_scopes  TEXT[]      NULL,
    status           TEXT        NOT NULL DEFAULT 'active',
    last_jwks_ok_at  TIMESTAMPTZ NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_identity_issuers_status_check CHECK (status IN ('active', 'disabled')),
    CONSTRAINT agent_identity_issuers_issuer_no_pipe CHECK (position('|' in issuer) = 0),
    CONSTRAINT agent_identity_issuers_jwks_https CHECK (jwks_uri LIKE 'https://%'),
    CONSTRAINT agent_identity_issuers_agent_issuer_unique UNIQUE (agent_id, issuer)
);

-- One ACTIVE owner per issuer string: the agent↔issuer binding must be a function of the issuer.
CREATE UNIQUE INDEX IF NOT EXISTS agent_identity_issuers_active_issuer_uidx
    ON agent_identity_issuers (issuer)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS agent_identity_issuers_agent_idx
    ON agent_identity_issuers (agent_id, status);
