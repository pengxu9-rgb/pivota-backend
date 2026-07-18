# ADR-015: Canonical Authorized Action — one authorization object across mandates, consent scope, and delegated tokens

**Status:** Proposed
**Date:** 2026-07-18
**Deciders:** Founder (peng) — authorization/trust; Security / Trust / Commerce owners
**Part of:** the agent-protocol interoperability model — **ADR-014** (meta). ADR-015 is its **authorization-layer** instance (ADR-014 action item 4): the canonical object that "what did the human authorize" resolves to, and the single gate that enforces it.
**Builds on:** ADR-012 (which built the AP2 Intent→Cart→Payment **mandate** authority — the AP2-specific precursor this generalizes). Companion: ADR-013 (settlement consumes the authorized amount and decrements the budget in the same transaction); `docs/AP2_ENABLEMENT.md`.
**Scope:** the authorization layer only — the **Canonical Authorized Action (CAA)** object and the deny-by-default gate that checks "the concrete transaction falls within the CAA." Out of scope: identity (ADR-012), settlement (ADR-013), discovery (ADR-007).

---

## Context

ADR-014 named four canonical layers and flagged **authorization** as the only one with no owning ADR — and the one with the most security weight (a gap here means an agent moving *more money, to a different merchant,* than the human authorized). This ADR owns it.

The layer has three native authorization primitives across Pivota's rails — all answering the *same* question in different encodings:
- **AP2 mandates** — signed Intent→Cart→Payment Verifiable Credentials (ADR-012).
- **Consent scope** — an API-key/AP2 consent with `scope.actions` + `spending_limit` (`agent_consents`, migration 021).
- **Delegated payment tokens** — the ACP shape (a Stripe Shared Payment Token carrying amount/merchant/expiry); no abstraction in the codebase yet.

### Current state (grounded — and it is inconsistent)
- **The mandate path binds, but inline.** `initiate_transaction` verifies the mandate chain and binds it to the concrete transaction — `cart.merchant_id == payment.merchant_id`, `cart.total == payment.amount`, `cart.currency == payment.currency` (`routes/ap2_routes.py:354-359`). Correct, but the logic lives in the route, not a reusable gate.
- **The consent-scope path enforces nothing on the transaction.** `agent_consents` carries `scope`, `spending_limit`, `spent_amount` (migration 021), and `consent_service.validate_consent` checks *action ∈ scope* **and** *amount ≤ (spending_limit − spent_amount)*. **But nothing calls it.** Both AP2 transaction routes (`initiate` :311, `confirm` :458) call only `verify_consent`, which returns identity + scope and enforces *neither* the action *nor* the limit. (`wallet/balance` :636 also calls only `verify_consent`, but it is a read, not the pay path — and [PR #1471] 501s it anyway.) So a plain consent→pay authorizes on **token validity alone**. Worse, the correct pieces *exist but are dead code*: `validate_consent` (action + limit) has **zero callers**, and so does `increment_usage` (the `spent_amount += amount` tracker) — so even the spending limit that is written at grant time is never enforced or accumulated.
- **ACP** would add a *third* authorization enforcement path if built as its own vertical.

So authorization is enforced inconsistently (mandate: bound; consent: not; ACP: absent), the enforcement primitives that *do* exist are unwired, and there is **no canonical object and no single gate.** This is the worst layer to leave in that state.

### Forces at play
- **It is the same economic question regardless of protocol:** did the human permit *this agent* to do *this action*, for *this amount*, at *this merchant*, within *this window*, in *this mode* (present / off-session)? The encodings differ; the envelope does not.
- **The invariant is deny-by-default and "transaction ⊆ authorization"** — and the transaction's economic parameters must be **derived from or validated equal to** the authorization, never trusted from separate caller-supplied fields (else an agent authorizes $10 to merchant X and executes $1000 to merchant Y).
- **Highest severity, so consistency is non-negotiable.** Re-implementing this invariant per protocol (the status quo) multiplies the audit surface and is already how the consent-path gap arose.
- **Presence is security-relevant, not cosmetic.** An off-session (agent-in-chat, no human) capture is a *merchant-initiated transaction* in card-network terms and requires prior cardholder agreement — so whether off-session was authorized must be part of the authorization, not assumed.

## Decision

**Adopt Option A: define one Canonical Authorized Action (CAA) object that every protocol's `verify_authorization` (the ADR-014 authorization port) produces, and a single deny-by-default authorization gate in the core that enforces "the concrete transaction falls within the CAA" for every rail. The transaction's economic parameters are derived from or checked equal to the CAA — never trusted from separate caller-supplied request fields.**

Concretely:

1. **The CAA is the canonical output of the authorization layer.** Each adapter maps its native primitive (mandate chain / consent scope / delegated token) → CAA; the core never sees the native primitive at enforcement time.
2. **One gate enforces the invariant** (below) for every protocol — replacing the inline mandate binding and wiring the currently-dead consent enforcement.
3. **Presence is a first-class CAA field** consumed by the ADR-014 core pipeline (off-session capture + kill-switch).
4. **Budget CAAs decrement atomically at settle** (ADR-013), closing the no-spend-tracking gap; single-use CAAs are consumed (nonce / cart_hash) so replay/concurrent double-spend can't exceed the authorization.
5. **Refund/reversal is a distinct action** requiring its own CAA — a `pay` authorization never authorizes a `refund`.

### The Canonical Authorized Action (schema + per-protocol mapping)

| CAA field | Meaning | AP2 mandate | Consent scope | Delegated token (ACP) |
|---|---|---|---|---|
| `agent_id` | authorized agent (canonical, ADR-012) | mandate **subject** DID → agent | `consent.agent_id` | token → agent |
| `authorizer` | the human/account that authorized (audit) | Intent **issuer** DID | account owner | cardholder |
| `action` | `pay` / `refund` / … | mandate action (`create_payment`) | `scope.actions` | token scope |
| `amount` | **exact** authorized amount (bound) | `Payment.amount == Cart.total` | — (ceiling only) | token amount |
| `max_amount` | **ceiling** (open authorization) | `Intent.constraints.max_amount` | `spending_limit − spent_amount` | token amount |
| `currency` | | `Cart.currency` | consent currency | token currency |
| `merchants` | allowed merchant(s) | `Cart.merchant_id` ⊆ `Intent.merchants` | consent merchants (if scoped) | token merchant |
| `product_ref` | optional cart/product binding | `Cart.cart_hash` | — | token line items |
| `presence` | `on_session` / `off_session` | mandate-declared | consent-declared | token type (CIT/MIT) |
| `single_use` | consumed on use vs reusable-in-window | cart/nonce bound | reusable within window+limit | per token |
| `not_before` / `expires_at` | validity window | mandate window | consent expiry | token expiry |
| `source_protocol` + `authorization_ref` | provenance (non-repudiation) | mandate chain id | `consent_id` | token id |

### The enforcement gate (the invariant)

For every transaction, in the core, deny by default:
- **No verified CAA ⇒ reject.** (Closes today's "valid token ⇒ allowed" consent gap.)
- `transaction.action == CAA.action`; `transaction.merchant ∈ CAA.merchants`; `transaction.currency == CAA.currency`.
- **Amount:** if `CAA.amount` is set (bound), `transaction.amount == CAA.amount` (exact); else `transaction.amount ≤ CAA.max_amount` (ceiling). Exactly one of the two must be set.
- **Time:** `not_before ≤ now < expires_at`.
- **Presence:** an off-session capture requires `CAA.presence == off_session` — an on-session-only authorization cannot back a merchant-initiated charge.
- **Consume atomically:** single-use CAAs are consumed (nonce/cart_hash); budget CAAs decrement `spent_amount` in the **same DB transaction as settlement** (ADR-013), so replay/concurrency can't exceed the authorization.
- **Source of truth:** the executed transaction's economic fields come from the CAA (or are validated equal to it); a caller-supplied amount/merchant that disagrees is a **rejection, not an override.**

## Options Considered

### Option A: One Canonical Authorized Action + a single core gate — this ADR
| Dimension | Assessment |
|---|---|
| Consistency | **High** — the highest-severity invariant enforced + audited once |
| Complexity | **Med** — design the CAA precisely; extract/wire one gate |
| Fit with ADR-014 | **High** — it *is* the authorization port's canonical output |
| Reversibility | **High** — the gate is additive; adapters map onto it |

**Pros:** one place to reason about "can this agent move this money"; the CAA is exactly what the ADR-013 settlement gate and ADR-014 pipeline consume; new protocols map to the CAA with the gate untouched; **fixes the live consent-path gap and revives the dead `validate_consent`.**
**Cons:** requires designing the CAA carefully (exact-vs-ceiling amount, presence, single-use); a genuinely novel authorization primitive that doesn't reduce to the envelope forces a CAA extension.

### Option B: Per-protocol authorization enforcement (status quo trajectory)
Each rail verifies and enforces its own authorization; the core gets a boolean.
| Dimension | Assessment |
|---|---|
| Consistency | **Low** — the invariant is re-implemented per protocol |
| Complexity | Low per-protocol, **High**/risky in aggregate |

**Pros:** each protocol ships in isolation.
**Cons:** re-implements the highest-risk invariant *k* times, inconsistently — **this is already how the gap arose** (mandate bound, consent unbound, `spent_amount` untracked). The audit surface multiplies exactly where it must not. **Rejected.**

### Option C: Adopt AP2's mandate model as the canonical authorization representation
Make the Intent→Cart→Payment mandate the canonical object; synthesize mandates for consent/ACP.
| Dimension | Assessment |
|---|---|
| Integration | N−1 (AP2 native) |
| Core coherence | **Med** — coupled to AP2's VC spec |

**Pros:** aligns to the richest real standard; AP2 needs no mapping.
**Cons:** couples the core's most security-sensitive object to AP2's still-evolving VC spec; consent scope and delegated tokens do **not** naturally form Intent/Cart/Payment chains, forcing awkward synthetic mandates. Same reasoning as ADR-014 Option D, at the layer least able to afford spec churn. **Rejected** in favor of a vendor-neutral CAA.

## Trade-off Analysis

The invariant ("transaction ⊆ what the human authorized") is **identical across protocols and the highest-severity one in the system.** Centralizing it once (A) is the only way to enforce and audit it consistently; the CAA is simply the vocabulary that lets one gate serve every rail. Option B is not a neutral alternative — it is the **current bug**: the mandate path binds and the consent path doesn't, because each rail owns its own enforcement. Option C would centralize, but by conscripting one external spec for the layer with the least tolerance for external churn. Decisive factor: **one gate, one object, deny-by-default — because "did the human authorize this money movement" must have exactly one answer, computed the same way for every protocol.**

## Consequences

**Becomes easier**
- One place to reason about and audit "can this agent move this money"; the CAA is what ADR-013 settlement and the ADR-014 pipeline consume.
- New protocols map their primitive → CAA; the gate is untouched.
- Closes the live consent-path authorization gap and revives `validate_consent`.
- Non-repudiation: `source_protocol` + `authorization_ref` persisted on every transaction.

**Becomes harder / must be owned**
- Designing the CAA precisely — exact-vs-ceiling amount, presence semantics, single-use — is subtle, security-critical work.
- Wiring the gate into `initiate`/`confirm` (today only the mandate path binds) and **extracting** the inline binding (`ap2_routes.py:354-359`) into it.
- **Atomic spend-tracking:** `spent_amount` must be decremented in the same DB transaction as settlement (ADR-013) — it is a correctness requirement, not a nicety.
- **Presence ↔ card-network rules:** off-session = merchant-initiated (MIT) needs prior cardholder agreement; the CAA must carry it and the gate must enforce it — a real compliance detail.
- Refund needs its own `action=refund` CAA path.

**Must revisit if**
- A protocol introduces an authorization primitive that doesn't reduce to the envelope (e.g. streaming/metered or usage-based authorization) → extend the CAA rather than fork the gate.

## Action Items

1. [ ] **Founder / Security sign-off.** Move to Accepted; ticks ADR-014 action item 4.
2. [ ] **Define the CAA object** — pilot-minimal subset first: `agent_id`, `action`, `amount | max_amount`, `currency`, `merchants`, `presence`, `expires_at`, `single_use`, `source_protocol` + `authorization_ref`.
3. [ ] **Build the single gate + wire it:** extract the inline mandate binding (`ap2_routes.py:354-359`) into the gate, and route the plain consent path through it — wiring `consent_service.validate_consent` (action + `spending_limit`), which is currently dead code w.r.t. payments.
4. [ ] **Atomic spend-tracking:** wire the existing `consent_service.increment_usage` (`agent_consents.spent_amount += amount`), currently dead code, into settlement — in the **same DB transaction** as the ADR-013 debit; prevents replay/concurrent over-spend.
5. [ ] **Presence:** make `presence` a CAA field; the ADR-014 core pipeline uses it for off-session capture + the kill-switch; enforce that off-session capture requires an off-session-authorized CAA (MIT).
6. [ ] **Refund/reversal:** define a distinct `action=refund` CAA; a `pay` authorization never authorizes a refund (aligns with ADR-013's reversal action item).
7. [ ] **ACP mapping (when the ACP adapter lands, ADR-014):** map the delegated token → CAA; no separate enforcement path.
8. [ ] **Audit:** persist `source_protocol` + `authorization_ref` on the transaction record for non-repudiation.

## Rollback

Authorization-layer. For the **consent path** the gate is **strictly tightening** — it authorizes on token validity alone today, so the gate only denies what was already wrongly allowed (fail-closed, no regression risk). Two caveats keep this from being *globally* "strictly tightening" and must be handled at cutover: (a) the **mandate path already binds** amount/merchant/currency inline (`ap2_routes.py:354-359`), so folding it into the CAA gate is a **behavior-preserving refactor** — pin it with characterization tests first (per ADR-014 item 6) so the extraction cannot silently *loosen* the highest-severity path; and (b) the new deny conditions (`presence == off_session` required for an off-session capture; single-use consumption) can **newly reject currently-passing transactions** if CAAs are not populated with `presence`/`single_use` — an availability risk to stage carefully. Reversing means reverting to per-protocol enforcement (Option B) — but the consent-path gap (item 3) is a **live correctness issue** this ADR fixes regardless of the broader framework decision.
