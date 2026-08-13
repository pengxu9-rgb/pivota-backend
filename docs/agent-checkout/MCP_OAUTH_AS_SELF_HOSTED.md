# Self-hosted MCP OAuth Authorization Server (no third party)

**Status (2026-06-09):** built + tested (26 tests: 12 crypto core, 9 protocol flow, 5 router e2e),
additive and flag-gated (`MCP_OAUTH_AS_ENABLED`). Not yet enabled. Pairs with the Agent-side
resource server (`feat/mcp-oauth-resource-server` in PIVOTA-Agent).

## Why self-hosted

Pivota already has the only parts that are hard to build: an interactive **buyer login**
(`/auth/login` email OTP + `/auth/password/login`, cookie sessions over `shop_users`) and a
**consent model**. So we don't need Stytch/WorkOS/Auth0 — Pivota is its own identity provider end
to end. A managed provider would only re-buy the login + consent we already own and move buyer
identity into someone else's tenant.

## Architecture

```
Claude/ChatGPT/Gemini ──OAuth──▶  pivota-backend  = AUTHORIZATION SERVER (this branch)
                                   (DCR, /authorize reusing buyer login+consent, /token, JWKS)
        │ access token (RS256, aud=/mcp resource, sub=buyer)
        ▼
   PIVOTA-Agent /mcp  = RESOURCE SERVER (feat/mcp-oauth-resource-server)
        verifies the token via the AS JWKS → user_ref → safety kernel → checkout
```

## Endpoints (all 404 unless `MCP_OAUTH_AS_ENABLED=1`)

- `GET /.well-known/oauth-authorization-server` — RFC 8414 metadata
- `GET /.well-known/jwks.json` — public signing key
- `POST /oauth/register` — Dynamic Client Registration (RFC 7591); MCP clients are public + PKCE
- `GET /oauth/authorize` — validates request (PKCE S256 + `resource` required); if no buyer session →
  redirect to `MCP_OAUTH_AS_LOGIN_URL?next=…`; else renders consent; HMAC-signs the request blob
- `POST /oauth/authorize/decision` — buyer approves → issues a single-use auth code → redirect
- `POST /oauth/token` — `authorization_code` (PKCE verified) and `refresh_token` (rotating)

Security properties (all unit-tested): PKCE S256 only (plain refused), single-use codes (atomic
consume), code bound to client + redirect_uri + expiry, refresh rotation w/ reuse-revoked,
confidential-client secret auth, codes/refresh stored as sha256 only, extra claims can't override
registered claims.

## Env to set on pivota-backend (the AS)

```
MCP_OAUTH_AS_ENABLED=1
MCP_OAUTH_AS_ISSUER=https://api.pivota.cc            # must match the iss the RS trusts
MCP_OAUTH_AS_PRIVATE_KEY_PEM=<RSA private key PEM>   # REQUIRED in prod (stable JWKS across instances)
MCP_OAUTH_AS_KEY_ID=pivota-mcp-as-1
MCP_OAUTH_AS_REQUEST_SECRET=<random>                 # HMAC for the consent step (or reuse CONFIRMATION_SECRET)
MCP_OAUTH_AS_ALLOWED_RESOURCES=https://commerce.mcp.pivota.cc/mcp  # REQUIRED; comma-separated exact URLs; unset = every authorize fails invalid_target
MCP_OAUTH_AS_LOGIN_URL=https://<accounts-login-ui>   # must honor ?next= and set the accounts cookie
```

Note on removing an entry from `MCP_OAUTH_AS_ALLOWED_RESOURCES`: the allowlist is enforced at
/oauth/authorize only. Refresh tokens already issued for a removed resource keep re-minting
access tokens with that audience for up to 30 days — removal is NOT an instant kill switch.
To revoke immediately, also mark that resource's rows revoked in `mcp_oauth_refresh`.

Generate the key once:
`openssl genrsa 2048` → set as `MCP_OAUTH_AS_PRIVATE_KEY_PEM` (literal `\n` escapes are accepted).

## Env to set on PIVOTA-Agent (the resource server) to trust this AS

```
MCP_OAUTH_ENABLED=1
MCP_OAUTH_RESOURCE=https://pivota-agent-production.up.railway.app/mcp
MCP_OAUTH_AUTHORIZATION_SERVERS=https://api.pivota.cc
MCP_OAUTH_ISSUERS_JSON=[{"iss":"https://api.pivota.cc","jwksUri":"https://api.pivota.cc/.well-known/jwks.json","algs":["RS256"]}]
```

(The backend's own `agent_user_jwt` verifier, if used, is similarly pointed at the AS via
`AGENT_USER_JWT_ISSUERS` + `AGENT_USER_JWKS_URL`.)

## Deploy order

1. Merge + deploy this AS branch and the Agent resource-server branch.
2. Set the AS env (incl. the RSA key) on pivota-backend; the Agent env on PIVOTA-Agent.
3. Smoke: `GET https://api.pivota.cc/.well-known/oauth-authorization-server` → 200; `…/jwks.json` → 200.
4. Connect a real Claude/ChatGPT custom connector to `https://…/mcp` → it discovers the AS, runs
   DCR + the buyer login + consent, gets a token, and transacts keyless.

## Open review items (before live paid traffic — run the adversarial loop)

1. **Buyer subject ↔ user_ref / acp_session_id.** The AS sets `sub` = accounts `user_id`. Confirm
   the Agent's `acp_session_id` binding under OAuth (token session claim → else `Mcp-Session-Id`) is
   stable per logical checkout, or charge-once isolation degrades. (Open item shared with the RS.)
2. **`user_ref` derivation parity.** Backend `agent_user_jwt` derives `{iss}:{sub}`; the Agent
   `userTokenVerifier` derives `usr_<sha256(iss|sub)>`. Each consumer is internally consistent, but
   unify if the same token is used on both rails.
3. **Login UI `?next=` contract.** `MCP_OAUTH_AS_LOGIN_URL` must round-trip back to `/oauth/authorize`
   and set the accounts cookie on the AS domain.
4. **Key management.** Rotate `MCP_OAUTH_AS_PRIVATE_KEY_PEM` via the JWKS (publish both during overlap).
5. Keep `AGENT_CHECKOUT_STRICT_SUBMIT_PAYMENT_ENABLED=0` until the keyless paid canary is run.
