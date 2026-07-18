# AP2 Surface Enablement Runbook

The AP2 (Agent Payment Protocol v2) surface — `routes/ap2_routes.py` plus
`middleware/ap2_security.py` — is fully built and **gated off by default**.

- **Router:** mounted only when `settings.enable_ap2_routes` is true
  (`ENABLE_AP2_ROUTES` env, `config/settings.py`); see `main.py` (search
  `enable_ap2_routes`).
- **Middleware:** `AP2SecurityMiddleware` is always installed but constructed
  with `enabled=settings.enable_ap2_routes`, so it is an inert passthrough until
  the flag flips and never touches non-`/ap2/*` traffic.

With `ENABLE_AP2_ROUTES=false` (the default) nothing about request handling
changes anywhere. This runbook is the **go/no-go** for flipping it true.

## Status at a glance

**Engineering is complete** — every layer below shipped through adversarial
review and is on `main`. **What remains before a flip is operational** (schema +
provisioning) plus sign-off. Flipping the flag is the point of no return for this
surface; treat it as a gated change.

| | Item | State |
|---|---|---|
| ✅ | Identity, signature, header-contract, mandate authority (see "What's built") | DONE — merged & reviewed |
| ⬜ | **Apply schema** (migrations `021/022/023/182/183/184`) | ops — section 1 |
| ⬜ | **Provision agent identity** (`agents.did` per pilot agent) | ops — section 2 |
| ⬜ | **Provision agent wallets** (confirm path) | ops — section 3 |
| ⬜ | **`PLATFORM_SIGNING_KEY`** (receipt signing) | config — section 4 |
| ⬜ | **Reviewer + founder sign-off** (ADR-012 → Accepted) | governance |

## What's built and verified (no action needed)

- **`POST /ap2/consent/grant` self-authenticates and fails closed.** It resolves
  the agent's identity via `consent_service.get_agent_identity(agent_id)` and
  verifies `X-AP2-Signature` against the key resolved from it: unknown agent →
  `401`, no registered identity → `401`, bad signature → `401`, replayed nonce →
  `409`. It does **not** depend on middleware enforcement, so mounting the router
  is safe on its own.
- **Self-sovereign DID identity (ADR-012).** The agent's identity is a **DID**;
  the verification key is resolved *from* it — `did:key` offline (`services/ap2_did.py`),
  `did:web` fetched from the domain's `.well-known/did.json`
  (`services/ap2_did_web.py`: HTTPS-only, SSRF-guarded, redirects refused,
  streamed size-cap, bounded TTL cache, fail-closed). **`agents.did` is the
  source of record** (`get_agent_identity` prefers it; #1466); a DID-or-PEM in
  `agents.public_key` is a back-compat fallback. ES256 signatures are accepted in
  both ASN.1 DER **and** raw `r‖s`/JOSE form (#1458), so standards-based agents
  interoperate.
- **`verify_ap2_signature` reconciled (#1441).** The `Depends(verify_ap2_signature)`
  guard enforces one canonical signed-payload contract (`services/ap2_signing.py`),
  resolves the agent from the consent token, and verifies **before** consuming the
  nonce (no replay-DoS). Used by `transaction/initiate` and `transaction/confirm`.
- **Header contract — three tiers (#1468).** Middleware and routes agree; see the
  table in section 5. Writes require consent + signature + nonce; `wallet/balance`
  is a consent-only read; reads/bootstrap are public.
- **Mandate authority layer (ADR-012, optional per transaction).**
  `POST /ap2/transaction/initiate` accepts an optional Intent→Cart→Payment
  `mandate_chain`; when present it is verified (`services/ap2_mandate.py`:
  issuer-DID-signed, chain-linked, bound to the concrete transaction) against the
  agent's trusted issuers (`ap2_trusted_issuers`, #1461) — **deny by default**.
  Absent a chain, behavior is unchanged (consent-scope path). See section 2b.

## Pre-flip checklist

### 1. Apply the AP2 schema

The surface depends on tables/columns across several migrations — **all must be
applied**, or routes 500 (fail-closed) on first use:

| Migration | Provides |
| --- | --- |
| `021_ap2_security.sql` | `agent_consents`, `nonce_tracker`, `agents.public_key`, `agents.x402_enabled` |
| `022_wallet_infrastructure.sql` | `agent_wallets`, `merchant_wallets` |
| `023_x402_protocol.sql` | `x402_transactions`, `x402_exchange_rates` |
| `182_nonce_tracker_request_path.sql` | corrective — `nonce_tracker.request_path` (#1443) |
| `183_agents_did.sql` | `agents.did` (#1454) — the identity readers `SELECT did`; without it every AP2 read errors on the undefined column |
| `184_ap2_trusted_issuers.sql` | `ap2_trusted_issuers` (#1461) — the mandate authority registry (read by `transaction/initiate` when a mandate chain is present) |

**Not applied at boot** — the AP2 schema is `schema_guard`-exempt (surface is off
everywhere). Apply on demand via the by-number migration runner
(`routes/admin_run_migration_pending.py`, mounted at `/admin/migrations`):

- `GET /admin/migrations/pending/{NNN}` — inspect (no writes).
- `POST /admin/migrations/pending/{NNN}/run` with body `{"mode": "apply"}` —
  applies. Both require admin auth; the run endpoint **defaults to dry-run** (any
  `mode` ≠ `"apply"` writes nothing), so a missing `mode` silently no-ops.
  Postgres only.

Apply in order `021`, `022`, `023`, `182`, `183`, `184`. All are idempotent
(`ADD COLUMN`/`CREATE TABLE ... IF [NOT] EXISTS`) — safe to re-apply. Verify:

```sql
SELECT to_regclass('public.agent_consents')      IS NOT NULL AS agent_consents,
       to_regclass('public.nonce_tracker')        IS NOT NULL AS nonce_tracker,
       to_regclass('public.agent_wallets')         IS NOT NULL AS agent_wallets,
       to_regclass('public.x402_transactions')     IS NOT NULL AS x402_transactions,
       to_regclass('public.x402_exchange_rates')   IS NOT NULL AS x402_exchange_rates,
       to_regclass('public.ap2_trusted_issuers')   IS NOT NULL AS ap2_trusted_issuers,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='nonce_tracker' AND column_name='request_path') AS nonce_request_path,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='agents' AND column_name='did') AS agents_did;
```

### 2. Provision agent identity (`agents.did`)

**This is the prerequisite that makes the grant path work.** The grant fails
closed for any agent with no resolvable identity (`401 "…no registered public
key…"`). The `agents.did` column exists (migration 183) but is provisioned
per-agent as an enablement step — for the pilot, **admin-provisioned** (self-serve
proof-of-control enrollment is a later slice; this supersedes the old
[#1442](https://github.com/pengxu9-rgb/pivota-backend/issues/1442) PEM-upload plan).

For each pilot agent, set `agents.did` to its DID (via `db.agents.set_agent_did`
or SQL). Two DID methods:

- **`did:key`** — self-contained; the key *is* the identifier, resolved offline.
  Simplest for a pilot; no hosting, but the key can't rotate without a new DID.
- **`did:web`** — `did:web:<domain>[:path]`; Pivota fetches the agent's
  `.well-known/did.json` (or `<path>/did.json`) over HTTPS and resolves the
  verification method. Rotation-friendly (the agent owns its doc); requires the
  domain reachable from the app.

The agent signs consent/transaction requests with the DID's **private** key;
Pivota resolves the public key from the DID. Algorithm is authoritative from the
DID (`ES256` for EC P-256, `Ed25519`). The consent signed payload is the canonical
JSON of `{"agent_id","scope","duration_hours","nonce"}` — see
`services/consent_service.py::create_consent` and `_sign_consent_payload` in
`tests/test_ap2_consent_grant_route.py`. A raw ES256/Ed25519 PEM in
`agents.public_key` is the legacy fallback (`get_agent_identity` uses it only when
`agents.did` is unset). Verify:

```sql
SELECT agent_id, did, (did IS NOT NULL OR public_key IS NOT NULL) AS has_identity
FROM agents
WHERE agent_id IN ( /* AP2 pilot agent_ids */ );
```

#### 2b. (Only if the pilot uses mandate authority) trusted issuers

If pilot transactions carry an Intent→Cart→Payment `mandate_chain`, each agent's
**authorizing issuer DID(s)** must be registered in `ap2_trusted_issuers`
(`db.ap2_trusted_issuers.add_trusted_issuer(agent_id, issuer_did)`), else every
mandate is denied (empty set = deny by default). Not needed for the plain
consent-scope transaction path.

### 3. Provision agent wallets (confirm path)

`POST /ap2/transaction/confirm` authorizes the caller-supplied `X-Wallet-Address`
via `WalletService.verify_agent_wallet(agent_id, wallet_address)` (#1447): true
only when the address is registered to the agent (from the consent token) **and**
the row is `status = 'active'` in `agent_wallets` (column is `address`, not
`wallet_address`); else `403 "Wallet not authorized"`. `initiate` needs no wallet;
only `confirm` (and wallet/exchange routes, once implemented) do. Verify:

```sql
SELECT agent_id, network, address, status
FROM agent_wallets
WHERE agent_id IN ( /* AP2 pilot agent_ids */ )
ORDER BY agent_id;
```

### 4. Config: `PLATFORM_SIGNING_KEY` (receipt signing)

`crypto_service.sign_receipt` signs transaction receipts with
`PLATFORM_SIGNING_KEY`; if unset, receipt signing is disabled (logged at import).
Set it in the target env if the pilot consumes signed receipts. Not required for
grant/transaction auth.

### Resolved (no action — recorded for auditability)

- **`verify_ap2_signature` reconciliation** — ✅ #1441 (+ #1443 nonce_tracker
  schema, #1445 interval bind). One canonical contract + nonce-replay guard.
- **Middleware header contract** — ✅ #1468. Three tiers, middleware + routes in
  agreement (section 5).

## 5. Header contract (reference)

| Tier | Requires | Routes |
| --- | --- | --- |
| **public** | nothing | `status`, `protocols`, `consent/grant` (self-authenticating), `GET transaction/{id}`, `GET receipt/{id}`, `x402/quote` |
| **consent-only** | `X-Agent-Consent` (+ the route's own ownership check) | `GET wallet/balance` — a per-request nonce on an idempotent read would make each balance check single-use |
| **full (write)** | `X-Agent-Consent` + `X-AP2-Signature` + `X-AP2-Nonce` | `consent/revoke`, `transaction/initiate`, `transaction/confirm`, `x402/exchange` |

Enforced at the **route level** (`Depends(verify_ap2_signature)` — the real gate,
since the middleware is flag-gated) and mirrored by the middleware.
`x402/exchange` is `501`; when implemented it adopts the full signed contract.

## Reviewer / owner sign-off (required before the flip)

Do not set `ENABLE_AP2_ROUTES=true` in any shared environment until each owner
confirms their item, and record the sign-offs (PR approval, deploy ticket, or
change log) so the flip is auditable:

- [ ] **Schema applied** — migrations `021/022/023/182/183/184` applied and the
      section-1 verify SQL is all true. (Section 1)
- [ ] **Agent identity provisioned** — every pilot agent has `agents.did` (or a
      legacy PEM) set; the section-2 verify SQL returns `has_identity = true`. If
      mandates are used, `ap2_trusted_issuers` is populated. (Section 2 / 2b)
- [ ] **Agent wallets provisioned** — every pilot agent that will `confirm` has an
      `active` `agent_wallets` row. (Section 3)
- [ ] **`PLATFORM_SIGNING_KEY`** set if signed receipts are needed. (Section 4)
- [x] **`verify_ap2_signature` reconciliation** — ✅ #1441.
- [x] **Header contract** — ✅ #1468.
- [ ] **Founder sign-off** — ADR-012 moved to **Accepted**.
- [ ] **Deploy/on-call** — deploying engineer acknowledges the rollback path
      (flag false + redeploy) before the flip.

## Enablement steps

1. Apply the schema (section 1) and confirm the verify SQL is all true.
2. Provision `agents.did` for the pilot agents (section 2); if using mandates,
   populate `ap2_trusted_issuers` (2b).
3. Provision `active` `agent_wallets` rows for agents that will `confirm`
   (section 3).
4. Set `PLATFORM_SIGNING_KEY` if signed receipts are needed (section 4).
5. Collect the sign-offs above (including founder ADR-012 acceptance).
6. Set `ENABLE_AP2_ROUTES=true` and redeploy.
7. Smoke check end-to-end:
   - `GET /ap2/status` → 200.
   - `POST /ap2/consent/grant` (valid signature, provisioned agent) → 200 with a
     `consent_token`.
   - `POST /ap2/transaction/initiate` (consent + signature + nonce) → 200
     `pending`. (With a `mandate_chain`: a valid, trusted, bound chain → 200; an
     untrusted/unbound chain → 403.)
   - `POST /ap2/transaction/confirm` (same, `X-Wallet-Address` = the agent's
     active wallet) → 200 `completed`; an unregistered address → 403.

## Rollback

Set `ENABLE_AP2_ROUTES=false` and redeploy. The router unmounts and the middleware
reverts to an inert passthrough; no `/ap2/*` route remains reachable. No schema is
dropped (it is additive and idempotent), so re-enabling needs only the flag.
