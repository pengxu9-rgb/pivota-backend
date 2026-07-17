# AP2 Surface Enablement Checklist

The AP2 (Agent Payment Protocol v2) surface — `routes/ap2_routes.py` plus
`middleware/ap2_security.py` — is wired into the app but **gated off by default**.

- Router: mounted only when `settings.enable_ap2_routes` is true
  (`ENABLE_AP2_ROUTES` env, `config/settings.py`). See `main.py` (search
  `enable_ap2_routes`).
- Middleware: `AP2SecurityMiddleware` is always installed but constructed with
  `enabled=settings.enable_ap2_routes`, so it is a no-op passthrough until the
  flag flips, and it never touches non-`/ap2/*` traffic.

With `ENABLE_AP2_ROUTES=false` (the default) nothing about request handling
changes. This document is the checklist to work through **before** setting it
true in any environment.

## What already holds (verified)

- **`POST /ap2/consent/grant` self-authenticates and fails closed.** It resolves
  the agent's registered key via `consent_service.get_agent_public_key(agent_id)`
  and:
  - unknown agent → `401 "Unknown agent"`,
  - agent with no `public_key` → `401 "Agent has no registered public key…"`,
  - bad signature → `401 "Invalid signature"`,
  - replayed nonce → `409 "Nonce already used"`.
  It does **not** depend on `AP2SecurityMiddleware` enforcement, so mounting the
  router is safe on its own.
- **No route uses `Depends(verify_ap2_signature)`.** The only reference to that
  helper is a docstring example, so its known problems (below) do not affect any
  live route today.
- **The middleware exempts the consent bootstrap.** `/ap2/consent/grant` is in
  the middleware's public list, so enabling enforcement does not block the very
  endpoint that issues consent tokens.

## Blockers to clear before `ENABLE_AP2_ROUTES=true`

### 1. Register agent public keys (grant fails closed without them)

`db/migrations/021_ap2_security.sql` adds `agents.public_key` (and
`agents.x402_enabled`) but **no code path currently writes `agents.public_key`.**
Every other `public_key` in the codebase (`routes/agent_api.py`,
`routes/agent_payment_sdk.py`, `routes/admin_api.py`,
`routes/merchant_api_extensions.py`, the PSP adapters) is a **PSP config key**,
not the AP2 agent signing key — do not confuse them.

Consequence: until a key is stored for an agent, every `POST /ap2/consent/grant`
for that agent returns `401 "Agent has no registered public key…"`. The grant
path is correct to fail this way; the gap is upstream provisioning.

Before enabling, decide and implement how keys get registered. Options:

- **Add a registration path** (recommended): extend an existing agent-onboarding
  surface (e.g. `routes/agent_api.py` / `routes/agent_payment_sdk.py`, which
  already accept a `public_key` field in their payment-adapter payloads) to
  persist the agent's ES256/Ed25519 **signing** public key into
  `agents.public_key`. Keep it distinct from any PSP `public_key`.
- **Admin/manual backfill** for a pilot: `UPDATE agents SET public_key = :pem
  WHERE agent_id = :id` for the specific agents in the pilot cohort.

Verify before flipping the flag:

```sql
-- Agents that will use AP2 must have a key populated.
SELECT agent_id, (public_key IS NOT NULL) AS has_key
FROM agents
WHERE agent_id IN ( /* AP2 pilot agent_ids */ );
```

Keys must match the algorithm the agent signs with (`ES256` default, or
`Ed25519`; see `services/crypto_service.py::verify_agent_signature`), and the
signed payload is the canonical JSON of
`{"agent_id", "scope", "duration_hours", "nonce"}` (sorted keys, compact
separators) — see `services/consent_service.py::create_consent` and the
`_sign_consent_payload` helper in `tests/test_ap2_consent_grant_route.py`.

### 2. Reconcile `verify_ap2_signature` before any route adopts it

`middleware/ap2_security.py::verify_ap2_signature` (the `Depends` helper — not
the grant path) is **not** safe to attach to a route yet. Coordinate with the
sibling AP2 follow-up first:

- **Inconsistent signed-payload contract.** It verifies over
  `{**request_body, "nonce": nonce}` (the whole body), whereas the grant path /
  `create_consent` verify over the fixed canonical subset
  `{"agent_id", "scope", "duration_hours", "nonce"}`. An agent cannot satisfy
  both with one signature. Pick one contract and make both sides use it.
- **No nonce-replay check.** `verify_ap2_signature` verifies the signature but
  never records/consumes the nonce (`verify_ap2_nonce` is a separate call). A
  route guarded only by this helper would be replayable.

Until both are fixed, do not add `Depends(verify_ap2_signature)` to any route.

### 3. Reconcile the middleware header contract for the other write routes

The middleware requires `X-Agent-Consent` **and** `X-AP2-Signature` **and**
`X-AP2-Nonce` on every non-public `/ap2/*` route. Only `/ap2/consent/grant` is
exempted (item above). The remaining write routes are **not** all consistent
with that requirement — e.g. `POST /ap2/consent/revoke` sends only
`X-Agent-Consent`, so with enforcement on it would be rejected for a missing
signature/nonce. Before enabling, either align each route's header usage with
the middleware, or narrow the middleware's per-route requirements. Confirm the
intended contract for: `revoke`, `transaction/initiate`, `transaction/confirm`,
`x402/exchange`, `wallet/balance`.

### 4. Confirm the backing tables exist in the target environment

`agent_consents`, `nonce_tracker`, `x402_transactions`, `agent_wallets`,
`x402_exchange_rates`, plus the `agents.public_key` column — all from migration
021 (and the x402 migrations). Ensure they are applied in the environment before
enabling, or the routes will 500 on first use.

## Enablement steps (once blockers are cleared)

1. Apply migration `021_ap2_security.sql` (and x402 table migrations) to the
   target DB.
2. Register `agents.public_key` for the pilot agents (item 1) and verify with
   the SQL above.
3. Confirm items 2–4 are resolved for whichever routes the pilot will exercise.
4. Set `ENABLE_AP2_ROUTES=true` and redeploy.
5. Smoke check: `GET /ap2/status` → 200; `POST /ap2/consent/grant` with a valid
   signature for a registered agent → 200 with a `consent_token`.

## Rollback

Set `ENABLE_AP2_ROUTES=false` and redeploy. The router unmounts and the
middleware reverts to an inert passthrough; no `/ap2/*` route remains reachable.
