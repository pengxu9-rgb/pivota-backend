# ADR-012: Agent Transaction Rail Boundary — API-key ACP vs signed AP2/x402

**Status:** Proposed
**Date:** 2026-07-17
**Deciders:** Founder (peng) — positioning + Commerce/Trust owners
**Builds on:** ADR-007 (citable index vs commerce overlay — settled the *read* side), ADR-009 (seller-of-record identity). Companion: `docs/AP2_ENABLEMENT.md` (enablement checklist + blockers), issue [#1442](https://github.com/pengxu9-rgb/pivota-backend/issues/1442) (agent identity provisioning — re-scoped, see below).
**Scope:** the *transact/pay* leg for **external** agents, plus **how that rail roots agent identity**. This ADR does **not** touch discovery/read (ADR-007 owns that) and changes no serving behavior.

**Revisions:**
- *2026-07-17 (rev 2):* Review of rev 1 caught that the signed rail was still bootstrapped by a **platform-issued API key** (an uploaded PEM under `X-API-Key` auth) — contradicting the very "external/untrusted agent" premise the rail exists for, and diverging from where ACP/UCP/MCP/AP2 actually root identity (tokens / verifiable credentials, not platform secrets). Identity rooting is now a **first-class axis** of this ADR, decided as **DID/VC**, sequenced did:key → did:web → VC/mandates. The first slice (`did:key`) is implemented in [#1452](https://github.com/pengxu9-rgb/pivota-backend/pull/1452). See "Identity rooting" below. The rail-boundary decision (Option A) is unchanged.

---

## Context

ADR-007 (founder-confirmed 2026-06-24) settled the **read** half: external/frontier agents call the Pivota commerce index to **cite**, offer-free and un-gated (`index_eligible`); first-party Pivota Agent keeps its offer/seed gate for curated shopping (`transact_eligible`). "Open index for citation, curated gate for first-party transact."

What ADR-007 did **not** settle: when an **external** agent wants to **act** (buy), not just cite — what identity and payment rail does it use? The code today answers that question **twice**, with two overlapping rails:

**Rail 1 — API-key ACP (live).** `routes/agent_api.py` (mounted, ~10k lines), authenticated by an agent API key (`X-API-Key` → `db/agents.py::get_agent_by_key`). It carries the full journey:
- discovery: `/agent/v1/beauty/products/search`, `/products/resolve`, `/product-groups/resolve`, `/products/{merchant_id}/{product_id}`
- transact: `/cart/validate` → `/checkout/acp-session` → `/orders/create` → `/orders/{order_id}/confirm-payment` → track / refund / cancel
- payment backing: merchant PSP (Stripe et al.) via the existing order/checkout orchestration.

**Rail 2 — signed AP2 / x402 (dark-launched, off in every env behind `ENABLE_AP2_ROUTES`).** `routes/ap2_routes.py` + `middleware/ap2_security.py`, authenticated by a **per-request ES256/Ed25519 signature** over a canonical payload, with nonce-replay protection:
- `/ap2/consent/grant` (signed, scoped, nonce-guarded) → `/ap2/transaction/initiate` → `/ap2/transaction/confirm` (wallet-bound) → `/ap2/receipt/{id}` (platform-signed)
- payment backing: **agent wallets + x402 stablecoin** settlement (`agents.x402_enabled` = "can initiate X-402 stablecoin payments", migration 021). `/ap2/x402/exchange` is still `501 Not Implemented`.

**The problem:** these are two divergent payment paths for the same economic action. Rail 2's payment leg (`/ap2/transaction/*`) duplicates Rail 1's (`/orders/*`). If AP2 is enabled without deciding the boundary, we ship two order/payment systems with different identity models, different settlement backing, and different receipt semantics — and no rule for which an agent should use. And Rail 2's identity primitive had **no source at all** — `agents.public_key` was read but never written — so *how* that key is rooted was still fully open (and is the second decision this ADR makes; see "Identity rooting"). Both questions are cheap to settle now, before anything depends on either.

### Forces at play
- **Trust asymmetry.** Read/cite is safe to open maximally (ADR-007). *Money movement* is where non-repudiation, per-action authorization, and replay protection actually matter — which is exactly what AP2's signature + nonce + scoped consent provide and what a bearer API key does not.
- **Settlement rails differ.** Rail 1 = merchant PSP (fiat, card-present-ish). Rail 2 = agent wallet + stablecoin (x402). These are genuinely different money movements, not just different auth.
- **Caller trust differs.** Partner/first-party agents are onboarded, KYC-adjacent, and already hold API keys. Arbitrary third-party/frontier agents are not — a bearer key is a weak identity for them to move money with.
- **Don't fork discovery.** Whatever we decide, AP2 must not grow discovery endpoints — signing a product search buys nothing. Discovery stays on the ADR-007 read surface.

## Decision

**Adopt Option A: AP2/x402 is the signed rail for external + stablecoin-settled transactions; API-key ACP remains the rail for first-party/partner + PSP-settled transactions. Discovery stays unified on the ADR-007 read surface. The two payment legs stay explicitly separate and are selected by *caller class × settlement type*, not offered as interchangeable.**

Concretely:
- **Identity.** AP2 identifies an agent by a **DID** and verifies each request against the key resolved from that DID (see "Identity rooting" below) — self-sovereign, rotatable, no platform-issued secret. API-key ACP keeps bearer-key identity. An agent on the AP2 rail is a *distinct, higher-assurance identity* even if it maps to the same `agent_id`.
- **Rail selection is a property of the caller + settlement, not a free choice:**
  - first-party Pivota Agent, and onboarded **partner** agents paying via **merchant PSP** → **API-key ACP** (`/orders/*`).
  - external/third-party agents, and/or **stablecoin (x402)** settlement → **AP2** (`/ap2/transaction/*`).
- **AP2 is not a general replacement** for the API-key order path. It is the rail where non-repudiation and/or wallet/stablecoin settlement are required. Revisit only if partner agents start demanding signed non-repudiation for PSP orders too (see Consequences).
- **No discovery on AP2.** External agents discover via the ADR-007 read surface (`get_pdp` / `agent_pdp_view` via MCP/UCP/ACP), then reference the resolved `product_id`/`merchant_id` when they authorize an AP2 transaction.

The intended external-agent journey:

> present a **DID** (identity resolves to the verification key) → **discover** via the read surface (ADR-007) → **grant AP2 consent** (signed, scoped, nonce) → **initiate** AP2 transaction referencing the resolved product/merchant → **confirm** against the agent wallet → **platform-signed receipt**. Later slices carry the user→agent authorization as a **verifiable-credential mandate** rather than an opaque scope list.

## Identity rooting (how the signed rail knows the agent)

This is the second decision in this ADR, added in rev 2. It is **orthogonal** to the rail boundary above: it governs *how Pivota comes to trust a key for an agent*, not *which rail carries the money*.

**Decision: root the signed rail in self-sovereign DID/VC identity — never a platform-issued API key.** `agent_id` maps to a **DID**; Pivota resolves the DID → verification key and checks the AP2 signature against it. Authorization (user→agent delegation) travels as **verifiable credentials / AP2 mandates** (Intent → Cart → Payment), validated against a trusted-issuer registry. `agents.public_key` demotes from "an uploaded secret" to "a place that may hold a DID (or a cached resolved key)". Sequenced in shippable slices:

| Slice | What | Status |
|---|---|---|
| **`did:key`** | Self-contained, offline-resolvable DID; the public key *is* the identifier. No network, no registry. | ✅ implemented — [#1452](https://github.com/pengxu9-rgb/pivota-backend/pull/1452) (`services/ap2_did.py`; `did:key` in `agents.public_key` resolves in the grant flow) |
| **`did:web`** | Domain-rooted DID resolved from the agent's `.well-known/did.json`; rotation owned by the agent. Resolver with SSRF guard, redirect refusal, TTL cache, fail-closed timeouts; `publicKeyMultibase` + `publicKeyJwk`. | ✅ implemented — [#1453](https://github.com/pengxu9-rgb/pivota-backend/pull/1453) (stacked on #1452) |
| **VC / mandate chain** | W3C Verifiable Credentials + AP2 Intent/Cart/Payment mandates + trusted-issuer registry — the authority layer. | 🟡 in progress — verification **primitive** shipped [#1455](https://github.com/pengxu9-rgb/pivota-backend/pull/1455) (`services/ap2_mandate.py`); chain linkage + registry + grant wiring remain |

### Identity-rooting options considered

- **(rejected) Platform API-key upload** — agent authenticates with a Pivota `X-API-Key` and uploads a PEM to `agents.public_key`. This was rev 1's implicit approach (and a drafted `#1442` endpoint). Rejected: it roots a *cryptographic-identity, non-repudiation* rail in a *platform bearer secret*, which (a) external/frontier agents don't hold, and (b) is exactly what ACP/UCP/MCP/AP2 move away from. May survive only as an explicit first-party/partner **pilot** backfill, never the external model.
- **(intermediate) JWKS** — `agent_id` → a JWKS URI + `kid`; Pivota fetches the key from the agent's JWKS. Federated and rotation-friendly; a reasonable bridge, and a subset of what `did:web` gives. Folded into the `did:web` slice rather than built separately.
- **(chosen) DID / VC** — self-sovereign identity, no platform secret, matches real AP2's identity layer (DIDs + verifiable mandates). Larger build, sequenced above so each slice ships independently.

**Why:** the entire reason the signed rail exists is non-repudiation for the *least-trusted* callers moving money. Rooting that in a secret Pivota hands out is self-defeating; rooting it in the agent's own DID/credential is the point.

### DID ↔ agent association (decided)

How does a DID relate to a Pivota `agents` row? **Decision: add a dedicated `agents.did` column (unique, indexed, nullable). `agent_id` stays the internal identifier; the DID is the agent's AP2 external identity.** The two are mapped, not merged.

On the AP2 rail the agent presents its **DID**; Pivota (a) resolves the DID → verification key (did:key/did:web, already shipped) and (b) resolves the DID → internal agent row via `agents.did`, from which it draws `x402_enabled`, wallet binding, and rate/quota limits. Consent and transaction rows continue to store the **internal `agent_id`** so every existing foreign key (`agent_consents.agent_id`, `agent_wallets`, `nonce_tracker`, `x402_transactions`) stays valid. `agents.public_key` reverts to *only* a raw-PEM pilot fallback; DID agents carry no stored key (the DID is the key source).

- **(chosen) Separate `agents.did` column.** Additive, non-breaking; keeps the API-key rail and all FKs intact; cleanly separates "internal account id" from "self-sovereign external identity." A single agent may hold both a bearer key (`agent_id` rail) and a DID (AP2 rail); the audit trail records which identity authorized each transaction.
- **(rejected) Make `agent_id` itself the DID.** Would rewrite the primary identifier and every FK, break the live API-key rail, and force a migration of all existing agents — a one-way door for no benefit, since the mapping column achieves the same external-identity property additively.

**Transitional note:** the shipped did:key/did:web slices store the DID in `agents.public_key` keyed by `agent_id` (no schema change needed to demo resolution). The `agents.did` column is the durable home; the grant flow re-keys onto DID-presented identity as a follow-up (does not block the resolver slices).

### VC / mandate authority layer (design)

Identity answers *who the agent is*; **mandates** answer *what the user authorized it to do*. AP2 carries that authorization as a chain of signed Verifiable Credentials, replacing today's opaque `scope` list:

| Mandate | Issuer → Subject | Authorizes | Key fields (beyond the common envelope) |
|---|---|---|---|
| **Intent** | user DID → agent DID | the agent to transact within limits | `constraints` = `actions`, `max_amount`, `currency`, `merchants`, validity window |
| **Cart** | user (or agent) DID → agent DID | a *specific* cart under an intent | `intent_ref`, `cart_hash`, `total`, `currency`, `merchant_id` |
| **Payment** | user DID → agent DID | the actual payment for a cart | `cart_ref`, `amount`, `currency`, `settlement` (psp \| x402) |

**Common envelope** (every mandate): `type`, `issuer` (DID), `subject` (DID), `issued_at`, `expires_at`, optional `nonce`; a **detached signature** over the canonical JSON, verified against the key resolved from the **issuer DID** — i.e. the authority layer reuses the *identity* layer (`services/ap2_identity.py`) and adds no new trust root.

**Chain linkage** (each step must reference and stay within the prior): `Cart.intent_ref == Intent.id` **and** `Cart.total ≤ Intent.constraints.max_amount` (and currency/merchant within intent); `Payment.cart_ref == Cart.id` **and** `Payment.amount == Cart.total`. Subject must be the **presenting agent's DID** at every step (binding), and every mandate must be unexpired.

**Trusted-issuer registry:** the issuer (the user's DID) must be one Pivota recognizes as the account's authorizing party — otherwise anyone with a resolvable DID could mint an intent. The registry maps *account → authorized issuer DID(s)* with revocation/status; the verification primitive already accepts a `trusted_issuers` set so the registry can be dropped in without touching the crypto.

**Wiring** (later slice): the grant/transaction flow accepts a mandate (or chain) instead of a raw `scope`; `verify_mandate` + `check_mandate_constraints` gate the action, and the enforced constraints — not a caller-supplied list — become the consent's authority. Kept out of the resolver PRs to avoid churning `consent_service` mid-review.

**Shipped vs remaining:** the verification **primitive** + constraint enforcement is in [#1455](https://github.com/pengxu9-rgb/pivota-backend/pull/1455) (single-mandate verify, fail-closed, DID-rooted). Remaining: chain-linkage validation, the trusted-issuer registry (+ revocation), and the grant-flow wiring.

## Options Considered (rail boundary)

### Option A: Two rails, boundary by caller-class × settlement (this ADR)
| Dimension | Assessment |
|---|---|
| Complexity | Medium — keeps both rails, adds an explicit selection rule + the #1442 identity primitive |
| Strategic fit | High — matches ADR-007's "different callers on different lanes"; non-repudiation lives exactly where money moves |
| Reversibility | High — AP2 stays flag-gated; boundary can widen later without a rewrite |
| Team familiarity | High — API-key rail unchanged; AP2 additive |

**Pros:** money-movement gets cryptographic identity + replay protection without imposing signing on every partner order; stablecoin/x402 gets a purpose-built rail; no forced rewrite of the live `/orders/*` path; consistent with the read/transact asymmetry ADR-007 already blessed.
**Cons:** two payment code paths to maintain (receipts, refunds, reconciliation must be coherent across both); a single agent could in principle hold both identities — needs a clear mapping; "caller class" must be defined precisely enough to route deterministically.

### Option B: AP2 as the strategic replacement for API-key payment auth
| Dimension | Assessment |
|---|---|
| Complexity | High — `/orders/*` must defer its payment leg to AP2; every partner agent must register a signing key and sign every action |
| Strategic fit | Medium — cleaner long-term identity story, but over-serves trusted partners |
| Reversibility | Low — migrating the live order path is a one-way door |
| Team familiarity | Low — new signing burden on all existing integrations |

**Pros:** one identity model (cryptographic) everywhere; single payment path long-term; strongest non-repudiation posture.
**Cons:** forces signing onto trusted partners who don't need it; large migration of a live, revenue-carrying path; couples AP2 enablement to a full ACP rewrite — exactly the coupling this ADR argues against; x402/wallet settlement isn't ready to be the only rail (exchange is `501`).

### Option C: Freeze AP2, extend API-key ACP for everything (incl. stablecoin)
| Dimension | Assessment |
|---|---|
| Complexity | Medium — bolt wallet/x402 settlement onto the API-key rail; retire `ap2_routes.py` |
| Strategic fit | Low — abandons cryptographic agent identity + the AP2/x402 protocol position |
| Reversibility | Low — throwing away merged, tested AP2 surface (#1439/#1440/#1441) |

**Pros:** one rail; no signing complexity; least new surface.
**Cons:** bearer API keys move stablecoin with no non-repudiation or replay protection — the weakest posture exactly where it matters most; discards a working, protocol-aligned surface; loses the external-agent trust story.

## Trade-off Analysis

The core trade-off is **identity strength vs integration burden, indexed to trust and settlement type.** ADR-007 already established that Pivota runs *different callers on different lanes*; the read lane is open, the first-party transact lane is gated. Option A extends that same principle to the transact lane for external agents: a **higher-assurance identity (signed) for higher-risk money movement (external caller and/or stablecoin)**, while leaving the trusted-partner PSP path on its existing, lower-friction bearer identity.

Option B is "purer" but pays for that purity by forcing signing onto everyone and migrating a live revenue path through a one-way door — the classic over-generalization mistake, and it drags AP2 enablement into a much larger blast radius. Option C optimizes for fewest moving parts but puts the weakest identity (bearer key) on the highest-risk action (stablecoin transfer), which contradicts the whole reason AP2 exists.

Decisive factor: **non-repudiation belongs where money moves and the caller is least trusted.** That is precisely the external/stablecoin quadrant, and precisely what AP2 already implements. So keep AP2 there; don't force it everywhere; don't discard it.

## Consequences

**Becomes easier**
- Enabling AP2 for a *pilot* cohort of external/stablecoin agents without touching the live partner order path.
- Reasoning about risk: signed rail ⇒ money-moving external agent; bearer rail ⇒ trusted partner PSP order.
- Rolling out agent identity in slices: `did:key` (done, #1452) needs no infra; `did:web` and VC/mandates land independently without touching the rail boundary.
- No platform secret to issue, store, or rotate for external agents — key rotation is the agent's own concern (its DID doc / JWKS), removing a whole class of credential-management burden.

**Becomes harder / must be maintained**
- **Two payment legs must stay coherent**: receipts, refunds, cancellation, and financial reconciliation have to reconcile `x402_transactions` (AP2) and the `/orders/*` PSP path into one economic ledger. This is the main ongoing cost of A and must be owned.
- **DID/VC infrastructure** beyond `did:key`: a `did:web` resolver means outbound HTTPS at verify time — needs caching, timeouts, and **fail-closed** behaviour on resolution failure. The VC/mandate slice needs a **trusted-issuer registry** and revocation/status checking. `did:key` (shipped) avoids all of this by being offline.
- **"Caller class" needs a precise definition** so routing is deterministic (e.g. an `agent_type` / trust-tier attribute, or `x402_enabled` as the switch). Ambiguity here re-introduces the "two interchangeable rails" problem.
- **DID ↔ agent record association** must be decided: is `agent_id` itself the DID, or does an `agents.did` column map one to the other? Until then, the pilot stores the DID in `agents.public_key`. A single `agent_id` may also hold both a bearer key and a DID — the audit trail must make the active rail + identity unambiguous per transaction.

**Must revisit if**
- Partner agents start requiring signed non-repudiation for PSP orders → widen AP2 toward Option B incrementally (partner-signed PSP orders), which A leaves open.
- x402/wallet settlement proves unviable (exchange execution stays unbuilt) → the external rail may need a PSP fallback, blurring the boundary.

## Action Items

1. [ ] **Founder sign-off** — on both decisions: the rail boundary (Option A — caller-class × settlement, AP2 *not* a general replacement for `/orders/*`) **and** DID/VC identity rooting. Move this ADR to Accepted on sign-off.
2. [ ] **Define "caller class" concretely** — decide the routing attribute (proposed: `agents.x402_enabled` and/or an agent trust-tier) that deterministically selects rail. Document it in `docs/AP2_ENABLEMENT.md`.
3. [x] **`did:key` identity slice** — offline DID resolution wired into the grant flow. Done: [#1452](https://github.com/pengxu9-rgb/pivota-backend/pull/1452) (`services/ap2_did.py`).
4. [x] **`did:web` identity slice** — DID-document resolution over HTTPS with SSRF guard + TTL cache, fail-closed. Done: [#1453](https://github.com/pengxu9-rgb/pivota-backend/pull/1453) (`services/ap2_did_web.py`, `ap2_identity.py`).
5. [x] **DID ↔ agent association — decided:** separate `agents.did` column (unique), `agent_id` stays internal + FK-consistent (see "DID ↔ agent association" above). Foundation (migration + ORM column + `get_agent_by_did` helper) in progress; grant re-keying onto DID-presented identity is the wiring follow-up. Re-scopes #1442.
6. [~] **VC / mandate authority layer** — verification primitive + constraint enforcement shipped ([#1455](https://github.com/pengxu9-rgb/pivota-backend/pull/1455), `services/ap2_mandate.py`); **remaining:** Intent→Cart→Payment chain linkage, trusted-issuer registry (+ revocation), and grant-flow wiring. Design in "VC / mandate authority layer" above.
7. [ ] **SSRF hardening for self-registration** — if agents self-register `did:web`, pin the resolved IP into the connection (close the check-then-fetch TOCTOU gap noted in `ap2_did_web.py`).
8. [ ] **Reconciliation owner** — assign ownership of the combined `x402_transactions` + `/orders/*` financial ledger before AP2 carries real money.
9. [ ] **Clear the remaining `AP2_ENABLEMENT.md` blockers** for the pilot scope (middleware header contract on `revoke`/`transaction/*`, `verify_ap2_signature` reconciliation, schema applied) — tracked separately; not gated by this ADR.
10. [ ] **Confirm discovery stays off AP2** — no discovery endpoints added to `ap2_routes.py`; external discovery remains on the ADR-007 read surface.

## Rollback

This ADR adds no runtime behavior on its own; AP2 remains gated by `ENABLE_AP2_ROUTES` (default false). Reversing the decision means not enabling the AP2 rail (leave the flag off) and, if desired, folding stablecoin settlement into the API-key rail (Option C) — a later, separate decision.
