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
- **`verify_ap2_signature` is reconciled, and the transaction write routes use it
  safely.** As of #1441 the `Depends(verify_ap2_signature)` helper enforces the
  same canonical signed-payload contract as the grant path
  (`services/ap2_signing.py`), resolves the agent from the consent token, verifies
  against the registered `agents.public_key`, and checks the signature *before*
  consuming the nonce (no replay-DoS). `POST /ap2/transaction/initiate` and
  `/ap2/transaction/confirm` adopt it. (This was Blocker 2 — see below.)
- **The middleware exempts the consent bootstrap.** `/ap2/consent/grant` is in
  the middleware's public list, so enabling enforcement does not block the very
  endpoint that issues consent tokens.

## Blockers to clear before `ENABLE_AP2_ROUTES=true`

### 1. Register agent public keys (grant fails closed without them)

> **Tracked in [#1442](https://github.com/pengxu9-rgb/pivota-backend/issues/1442).**
> This blocker is the prerequisite for the `ENABLE_AP2_ROUTES` flip; see that
> issue for the implementation plan and acceptance criteria.

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

### 2. ~~Reconcile `verify_ap2_signature` before any route adopts it~~ — ✅ RESOLVED (#1441)

**Resolved** by #1441 (with follow-ups #1443 — `nonce_tracker` schema reconcile,
and #1445 — `cleanup_old_nonces` interval param bind).
`middleware/ap2_security.py::verify_ap2_signature` now:

- **Uses one canonical signed-payload contract** shared with the grant path via
  `services/ap2_signing.py` — the earlier split (`{**body, "nonce"}` in the helper
  vs the canonical subset in `create_consent`) is gone, so an agent signs one
  payload for both paths.
- **Guards against nonce replay**, verifying the signature *before* the nonce is
  consumed so a bad signature can't burn a victim's nonce.

`POST /ap2/transaction/initiate` and `/ap2/transaction/confirm` now adopt
`Depends(verify_ap2_signature)`. No further action for this blocker.

### 3. Reconcile the middleware header contract for the other write routes

The middleware requires `X-Agent-Consent` **and** `X-AP2-Signature` **and**
`X-AP2-Nonce` on every non-public `/ap2/*` route. Only `/ap2/consent/grant` is
exempted (item above). The remaining write routes are **not** all consistent
with that requirement — e.g. `POST /ap2/consent/revoke` sends only
`X-Agent-Consent`, so with enforcement on it would be rejected for a missing
signature/nonce. Before enabling, either align each route's header usage with
the middleware, or narrow the middleware's per-route requirements. Confirm the
intended contract for: `revoke`, `x402/exchange`, `wallet/balance`.
(`transaction/initiate` and `transaction/confirm` were reconciled in #1441 — they
enforce consent + signature + nonce via `Depends(verify_ap2_signature)`,
consistent with the middleware.)

### 4. Confirm the backing tables exist in the target environment

`agent_consents`, `nonce_tracker`, `x402_transactions`, `agent_wallets`,
`x402_exchange_rates`, plus the `agents.public_key` column — all from migration
021 (and the x402 migrations). Ensure they are applied in the environment before
enabling, or the routes will 500 on first use.

### 5. Implement `WalletService.verify_agent_wallet` (transaction/confirm 500s without it)

`POST /ap2/transaction/confirm` calls
`wallet_service.verify_agent_wallet(agent_id, wallet_address)`
(`routes/ap2_routes.py`), but `WalletService` (`services/wallet_service.py`)
defines **no such method** (it has `get_agent_wallet`, `validate_address`, …), so
the call raises `AttributeError` and the route 500s. The current test
(`tests/test_ap2_transaction_signature.py`) monkeypatches the method into
existence, so CI is green while production has no implementation. Implement the
real authorization check (likely on top of `get_agent_wallet`) and drop the
monkeypatch before enabling.

Tracked in [#1448](https://github.com/pengxu9-rgb/pivota-backend/issues/1448).

## Reviewer sign-off (required before the `ENABLE_AP2_ROUTES` flip)

Flipping `ENABLE_AP2_ROUTES=true` is the point of no return for this surface —
treat it as a gated change that needs explicit sign-off, not a routine config
tweak. Do not set the flag true in any shared environment until each owner has
confirmed their blocker is cleared:

- [ ] **Agent key registration** — owner confirms a path writes `agents.public_key`
      (or the pilot cohort is backfilled) and the verify SQL returns `has_key = true`
      for every AP2 agent. (Blocker 1 — [#1442](https://github.com/pengxu9-rgb/pivota-backend/issues/1442))
- [x] **`verify_ap2_signature` reconciliation** — ✅ resolved in #1441: one
      canonical contract (`services/ap2_signing.py`) + nonce-replay guard; the
      transaction routes adopt the helper. (Blocker 2)
- [ ] **Middleware header contract** — owner confirms each non-public write route
      (`revoke`, `transaction/*`, `x402/exchange`, `wallet/balance`) is consistent
      with the middleware's required headers for the pilot scope. (Blocker 3)
- [ ] **Schema applied** — owner confirms migration 021 (+ x402 tables) is applied
      in the target environment. (Blocker 4)
- [ ] **Wallet authorization implemented** — owner confirms
      `WalletService.verify_agent_wallet` exists and `transaction/confirm` enforces it
      without a test monkeypatch. (Blocker 5 — [#1448](https://github.com/pengxu9-rgb/pivota-backend/issues/1448))
- [ ] **Deploy/on-call sign-off** — the deploying engineer acknowledges the rollback
      path (set the flag false + redeploy) before the flip.

Record the sign-offs (PR approval, deploy ticket, or change log) so the flip is
auditable.

## Enablement steps (once blockers are cleared)

1. Apply migration `021_ap2_security.sql` (and x402 table migrations) to the
   target DB.
2. Register `agents.public_key` for the pilot agents (item 1) and verify with
   the SQL above.
3. Confirm items 2–4 are resolved for whichever routes the pilot will exercise.
4. Collect the reviewer sign-offs listed above.
5. Set `ENABLE_AP2_ROUTES=true` and redeploy.
6. Smoke check: `GET /ap2/status` → 200; `POST /ap2/consent/grant` with a valid
   signature for a registered agent → 200 with a `consent_token`.

## Rollback

Set `ENABLE_AP2_ROUTES=false` and redeploy. The router unmounts and the
middleware reverts to an inert passthrough; no `/ap2/*` route remains reachable.
