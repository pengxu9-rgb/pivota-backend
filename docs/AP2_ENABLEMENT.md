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

### 4. Confirm the backing schema exists in the target environment

The AP2 surface depends on tables/columns spread across several migrations —
**all of them must be applied** before enabling, or the routes 500 (or wrongly
403) on first use:

| Migration | Provides |
| --- | --- |
| `021_ap2_security.sql` | `agent_consents`, `nonce_tracker`, `agents.public_key`, `agents.x402_enabled` |
| `022_wallet_infrastructure.sql` | `agent_wallets`, `merchant_wallets` |
| `023_x402_protocol.sql` | `x402_transactions`, `x402_exchange_rates` |
| `182_nonce_tracker_request_path.sql` | **corrective** — adds `nonce_tracker.request_path` that 021 omitted but the AP2 nonce paths INSERT (#1443). Idempotent (`ADD COLUMN IF NOT EXISTS`). |

**These are NOT applied at boot.** The AP2 schema is `schema_guard`-exempt (not
in `db/schema_guard.py`'s prod fast-mode startup) precisely because the surface
is disabled everywhere. Apply the full set **on demand via the admin migration
runner** (`routes/admin_run_migration.py` / `routes/admin_migrations.py`) as an
explicit enablement step — do not rely on startup self-heal. `182` is
idempotent, so re-applying it against an env that already has the column is safe.

Verify before flipping the flag:

```sql
SELECT to_regclass('public.agent_consents')      IS NOT NULL AS agent_consents,
       to_regclass('public.nonce_tracker')       IS NOT NULL AS nonce_tracker,
       to_regclass('public.agent_wallets')        IS NOT NULL AS agent_wallets,
       to_regclass('public.x402_transactions')    IS NOT NULL AS x402_transactions,
       to_regclass('public.x402_exchange_rates')  IS NOT NULL AS x402_exchange_rates,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='nonce_tracker' AND column_name='request_path') AS nonce_request_path,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='agents' AND column_name='public_key') AS agents_public_key;
```

### 5. Provision agent wallets for the confirm path

`POST /ap2/transaction/confirm` authorizes the caller-supplied `X-Wallet-Address`
via `WalletService.verify_agent_wallet(agent_id, wallet_address)` (added in
#1447). It returns true **only** when that address is registered to the agent
(resolved from the consent token) **and** the wallet row is `status = 'active'`
in `agent_wallets`; otherwise the route returns `403 "Wallet not authorized"`.
Note the column is `agent_wallets.address` (not `wallet_address`).

So for any pilot agent that will confirm transactions, an **active** wallet row
must exist. Until then, `confirm` fails closed at 403 (the signature/consent
checks still pass — this is authorization, not authentication). Verify:

```sql
-- Each pilot agent that confirms must have an active wallet on the network it uses.
SELECT agent_id, network, address, status
FROM agent_wallets
WHERE agent_id IN ( /* AP2 pilot agent_ids */ )
ORDER BY agent_id;
```

`initiate` does not need a wallet row; only `confirm` (and the wallet/balance /
exchange routes, once implemented) do.

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
- [ ] **Schema applied** — owner confirms migrations `021` + `022` + `023` + `182`
      are applied via the admin migration runner and the verify SQL returns true for
      every column/table (incl. `nonce_tracker.request_path`). (Blocker 4)
- [ ] **Agent wallets provisioned** — owner confirms every pilot agent that will
      `confirm` has an `active` row in `agent_wallets`, else confirm → 403. (Blocker 5)
- [ ] **Deploy/on-call sign-off** — the deploying engineer acknowledges the rollback
      path (set the flag false + redeploy) before the flip.

Record the sign-offs (PR approval, deploy ticket, or change log) so the flip is
auditable.

## Enablement steps (once blockers are cleared)

1. Apply the AP2 schema to the target DB via the admin migration runner —
   `021_ap2_security.sql`, `022_wallet_infrastructure.sql`,
   `023_x402_protocol.sql`, and the corrective `182_nonce_tracker_request_path.sql`
   — then run the Blocker 4 verify SQL and confirm every column/table is true
   (Blocker 4). It is not applied at boot; this step is mandatory.
2. Register `agents.public_key` for the pilot agents (Blocker 1) and verify with
   the SQL in that section.
3. Provision an `active` `agent_wallets` row for every pilot agent that will
   `confirm` (Blocker 5) and verify with that SQL.
4. Confirm Blocker 3 (middleware header contract) is resolved for whichever
   routes the pilot will exercise.
5. Collect the reviewer sign-offs listed above.
6. Set `ENABLE_AP2_ROUTES=true` and redeploy.
7. Smoke check end-to-end:
   - `GET /ap2/status` → 200.
   - `POST /ap2/consent/grant` (valid signature, registered agent) → 200 with a
     `consent_token`.
   - `POST /ap2/transaction/initiate` (consent + signature + nonce) → 200 `pending`.
   - `POST /ap2/transaction/confirm` (same, `X-Wallet-Address` = the agent's
     active wallet) → 200 `completed`; an unregistered address → 403.

## Rollback

Set `ENABLE_AP2_ROUTES=false` and redeploy. The router unmounts and the
middleware reverts to an inert passthrough; no `/ap2/*` route remains reachable.
