# ADR-014: Agent-Protocol Interoperability Model — one canonical spine, protocols as edge adapters

**Status:** Proposed
**Date:** 2026-07-18
**Deciders:** Founder (peng) — protocol strategy; Commerce / Trust / Platform owners
**Relationship:** **Meta-ADR.** Sits *above* and generalizes ADR-007 (read surface), ADR-012 (transaction rail boundary + identity rooting), ADR-013 (settlement model). Those three are the per-layer instances of the framework this ADR names; it does not override them.
**Scope:** the framework for supporting *multiple* agent-commerce protocols (AP2, x402, ACP, MCP, UCP, and successors) **together** — how they compose, where their code lives, and the one rule that keeps the core coherent. **Not** a decision to adopt or drop any specific protocol (those are the per-layer ADRs).

**Landscape note (confidence):** AP2 = Google (open, mandate-based, payment-agnostic); x402 = Coinbase (HTTP-402 stablecoin); ACP = OpenAI/Stripe (full-stack agentic checkout); MCP = Anthropic (model↔tool/context). In *this* repo, **UCP** reads as Pivota's own internal commerce surface (`UCP_INTERNAL_API_KEY`; "ACP and UCP API endpoints"), not a public standard — its status under this framework is decided below ("UCP's place": spine-side, no adapter). The framework does not depend on these attributions.

---

## Context

Agent commerce has no single winning protocol and won't soon. Already live or imminent: **MCP** (model↔tool/context), **ACP** (full-stack agentic checkout), **AP2** (signed mandate *authorization*, payment-method-agnostic), **x402** (stablecoin *settlement*), plus **UCP** (Pivota's own surface) — with Visa's Trusted Agent Protocol and Mastercard Agent Pay on the horizon. A merchant asks one simple question — "can an AI agent buy from me here?" — and the honest answer is "through whichever protocol the agent speaks." Pivota's position (ADR-007) is to be the commerce/trust layer *underneath* all of them. So **multi-protocol is not optional; it is the product.**

The protocols are commonly mistaken for competitors at one layer. They are not — they occupy **different layers of a single transaction**, and one purchase can compose several:

| Layer | Question | Where each protocol plays |
|---|---|---|
| **Discovery / read** | "find the product" | MCP · ACP feed · UCP · the API-key search rail — **unified by ADR-007** |
| **Identity** | "who is the agent" | AP2 → DID/VC · x402 → wallet key · ACP → platform/OAuth+Stripe · API-key → bearer — **axis of ADR-012** |
| **Authorization** | "what did the *human* authorize" | AP2 → Intent→Cart→Payment mandates (VC) · ACP → delegated payment token · API-key → consent scope |
| **Settlement** | "money moves" | x402 → stablecoin · ACP/API-key → Stripe PSP · AP2 → **agnostic** — **axis of ADR-013** |

So **AP2 is an *authorization* layer that rides on settlement rails including x402 and cards** (Google×Coinbase's AP2+x402 extension is exactly this) — "AP2 vs x402" is a category error. **ACP is the opposite shape** — a *full-stack* protocol bundling all four layers around Stripe. **MCP is discovery-only.** A realistic journey: *discover via MCP → authorize via an AP2 mandate → settle via x402* — three protocols, one purchase.

### What the code shows today
The convergence is **already happening — emergently, not by design:**
- A canonical execution core exists: `routes/payment_execution_routes.py` ("Unified Payment Execution Router"); `routes/payment_routing_routes.py::execute_payment_with_routing`.
- Protocol is already a first-class attribute of the canonical order: `order.metadata.protocol_name`, with protocol-aware guards (`services/agent_checkout_kill_switch.is_guarded_protocol`; `routes/agent_payment_sdk.py` reasons explicitly about "a protocol-tier (ACP/UCP/AP2) in-chat charge").
- A protocol-independent canonical concern has surfaced on its own: **presence** — off-session (agent in chat, no human for 3DS) vs client-present — which cuts across every protocol and drives the kill-switch.
- But `services/protocols/` is nearly empty (`pdp_direct.py`), and protocol handling is **scattered `protocol_name` string checks across payment routes.** There is no adapter contract and no single place that states how a protocol maps onto the core.

### Forces at play
- **The standards are unsettled and plural.** Betting on one is a coin flip; multi-homing is table stakes; new protocols will keep arriving.
- **N² is the failure mode.** Handling each protocol as its own vertical stack means every new protocol touches every payment path, and receipts/refunds/reconciliation diverge per protocol. ADR-012 already flags "two payment legs must stay coherent"; this is that problem generalized to *k* legs.
- **The layers are genuinely shared.** Identity, authorization, and settlement are the *same economic acts* regardless of wire format. The differences are at the edge (encoding, auth primitive, settlement rail), not the core.
- **Trust is the moat, and it's core-level.** Fraud guards, the kill-switch, attribution, non-repudiation, reconciliation — these must be enforced *once*, in the spine, or they leak and diverge per protocol.

## Decision

**Adopt Option A: a single Pivota-canonical, protocol-neutral commerce spine, with each protocol implemented as a thin edge adapter onto the spine's canonical layers — implementing only the per-layer ports for the layers it speaks. An adapter maps onto the canonical layers; it MUST NOT fork the core.**

Concretely:

1. **Four canonical layers, per-layer ports.** Discovery, Identity, Authorization, Settlement are the canonical seams, and each is its own **port**; a protocol implements **only the ports for the layers it speaks** (MCP: discovery only; AP2: identity + authorization, settlement-agnostic; ACP and the API-key rail: all four). The per-layer ADRs are the instances:
   - Discovery → **ADR-007** (one read surface; no protocol adds discovery endpoints). Discovery-only protocols (MCP, feeds) never enter the transact registry.
   - Identity → **ADR-012** (`agent_id` internal; DID / bearer / wallet-key are edge identities resolved to it).
   - Authorization → mandates / delegated tokens / consent scope all resolve to one canonical **authorized action** (amount, currency, merchant, scope, **presence** — whether a human is present to complete e.g. a 3DS step). **This layer has no owning ADR yet**; specifying the canonical authorized-action object is **ADR-015** (action item 4) — it is the hardest and most security-laden mapping in the framework.
   - Settlement → **ADR-013** (ledger / PSP / on-chain rails; the adapter *selects* a rail, the core *executes* it). **x402 lives here**: per ADR-012 it is AP2's stablecoin settlement extension — a rail selected via the settlement port, **not** a peer full-stack adapter.

2. **The spine owns the invariants.** The canonical order/transaction, the settlement ledger (ADR-013), fraud/kill-switch, attribution, receipts, and reconciliation live in the core and are enforced **once**, independent of protocol. The spine of record **today** is `payment_execution_routes`; the ADR-013 agent ledger joins it **once built** (ADR-013 is Proposed — the spine is partly aspirational until its phase 1 lands).

3. **Protocol = edge adapter behind one registry.** Replace scattered `protocol_name` string checks with an **adapter registry** keyed by protocol; a transact-capable protocol implements the transact-side port (below), and **unknown-protocol lookups fail closed** — blocked, never falling through to a default lane. Discovery-only protocols never enter the registry (ADR-007 governs them). Adding a protocol = adding an adapter, not editing the core.

4. **Vendor-neutral canonical model.** The spine is *Pivota's own* model; AP2 / ACP each get an adapter (x402 is a settlement rail, per point 1). Do **not** adopt an external protocol's schema as the internal representation (see Option D).

5. **A protocol-conformance matrix** (protocol × layer → native / adapted / n-a) is the auditable source of truth for "what we support," maintained beside `docs/AP2_ENABLEMENT.md` with a **named owner** and a PR-template update trigger.

### The transact-side port (implemented by protocols that speak the transaction layers)

This is the port behind the registry, covering the identity / authorization / settlement layers. Discovery has its own surface (ADR-007) and is not part of this contract — **MCP therefore implements no method below.** Names illustrative; the shape is the point — and the shape must be **derived from the three transact-capable shapes already live in the code** (API-key, ACP, AP2), not speculatively generalized for protocols that haven't arrived:

| Port method | Returns (canonical) | AP2 | ACP | API-key |
|---|---|---|---|---|
| `resolve_identity(req)` | canonical `agent_id` | DID→agent (ADR-012) | platform/OAuth→agent | bearer key→agent |
| `verify_authorization(req)` | authorized action (amount, ccy, merchant, scope, presence) | verify mandate chain | validate delegated token | consent scope |
| `to_canonical_order(req)` | canonical order/txn | from Payment mandate | from ACP session | from cart/checkout |
| `select_settlement(order)` | ADR-013 rail | ledger / x402 (phase 2) | Stripe PSP | merchant PSP |
| `render_receipt(txn)` | protocol receipt | platform-signed | ACP shape | order receipt |

**x402 appears only in the `select_settlement` row.** It is a settlement *rail* — an HTTP-402 challenge/response flow wrapped around resource access, not a request that arrives carrying an order — so modeling it as a standalone adapter would both contradict ADR-012 (which makes it AP2's settlement extension) and fit the port badly.

The core runs the **invariant pipeline** — fraud → kill-switch → presence/off-session enforcement → settle (ADR-013) → attribute → reconcile — between `verify_authorization` and `render_receipt`, **identically for every adapter.** Registry lookups for an **unknown or unregistered protocol fail closed** (blocked, exactly as the kill-switch treats guarded protocols), never falling through to a default lane.

### UCP's place (decided)

UCP is Pivota's **own** surface, not an external standard — so it is **spine-side** and gets **no adapter**: "adapting our own protocol to our own spine" is incoherent. Where existing code labels orders `protocol_name = ucp`, the registry treats that as a first-party lane of the spine, subject to the same invariant pipeline. If UCP is ever *published* as an external protocol for third parties to implement, the published version gets an adapter like any other protocol. Confirm this call at sign-off (action item 1).

## Options Considered

### Option A: Canonical spine + protocol edge adapters (ports & adapters) — this ADR
| Dimension | Assessment |
|---|---|
| Integration cost | **N** (one mapping per protocol) — scales to new protocols |
| Core coherence | **High** — trust/reconciliation invariants enforced once |
| Complexity | **Med** — must define the neutral model + adapter port up front |
| Reversibility | **High** — adapters are additive and removable |

**Pros:** absorbs new protocols by adding an adapter, not rewiring; one place for fraud/receipts/reconciliation; ADR-007/012/013 compose cleanly under it; smallest step from where the code already is (unified execution + `protocol_name`).
**Cons:** requires designing a vendor-neutral canonical model and a disciplined adapter boundary; the port is derived from only three live shapes, so expect one honest revision when the first genuinely external protocol lands; a genuinely novel protocol primitive that no canonical layer models forces a core extension, not just an adapter — a real, if rare, cost.

### Option B: Per-protocol vertical stacks (point-to-point) — the emergent status quo
Each protocol gets its own end-to-end path (own identity, order, settlement, receipts).
| Dimension | Assessment |
|---|---|
| Integration cost | **N²** — every protocol × every shared concern |
| Core coherence | **Low** — trust/reconciliation diverge per stack |
| Complexity | Low per-protocol, **High** in aggregate |

**Pros:** each protocol ships fast in isolation; no shared-abstraction design needed.
**Cons:** this *is* the mesh — ADR-012's "two payment legs must stay coherent" multiplied by *k*; fraud and reconciliation re-implemented (inconsistently) per protocol; the trust moat leaks. It is the trajectory today's scattered `protocol_name` checks are already on. **Rejected.**

### Option C: Monoculture — bet on one protocol
Pick the likely winner (ACP for ChatGPT distribution, or AP2 for open/Google) and implement only it.
| Dimension | Assessment |
|---|---|
| Integration cost | Lowest short-term |
| Reach | **Low** — excludes agents on every other protocol |
| Reversibility | **Low** — re-tooling when the bet is wrong |

**Pros:** simplest; deepest single integration.
**Cons:** standards are unsettled; excluding protocols excludes agent traffic, contradicting ADR-007's "be the layer underneath all of them." Premature commitment. **Rejected** — Pivota still *prioritizes* protocols, but via the conformance matrix, not by building only one.

### Option D: Adopt an external protocol's schema as the internal model
Make one protocol (say AP2's mandate model) the internal representation; translate the others into it.
| Dimension | Assessment |
|---|---|
| Integration cost | N−1 (the native protocol needs no adapter) |
| Core coherence | **Med** — coupled to one external spec |
| Reversibility | **Low** — the core churns with that protocol's versioning |

**Pros:** one fewer translation; aligns the core to a real standard.
**Cons:** couples the core to one still-evolving external spec; forces awkward mappings for differently-shaped protocols (x402's wallet-settlement doesn't fit ACP's Stripe-token model; ACP's full-stack session doesn't fit AP2's mandate model). A vendor-neutral spine (A) is more stable and treats all protocols evenly. **Rejected** in favor of A's own model.

## Trade-off Analysis

The decision is **N vs N² integration cost, and where the trust invariants live.** Option B (the status-quo trajectory) is cheapest for protocol #2 and ruinous by protocol #4 — and it scatters fraud/reconciliation, the one thing Pivota can't afford to fragment. Option C dodges the cost by giving up the reach the product is premised on. Option D is A with a shortcut that trades long-term core stability for one saved adapter — a bad trade while every candidate "canonical" protocol is still churning.

Option A pays a bounded up-front cost (define the neutral model + adapter port) to make protocol count a **linear, edge-local** concern and to enforce trust **once**. It is also the smallest move from where the code already is: `payment_execution_routes` is a proto-spine and `order.protocol_name` is a proto-adapter-key; A just makes them explicit before the mesh sets.

Decisive factor: **multi-protocol is the product, the protocols are unsettled, and the moat is core-level trust — so the core must be protocol-neutral and the protocols must be replaceable at the edge.**

## Consequences

**Becomes easier**
- Onboarding protocol #5 (Visa TAP, MC Agent Pay, …) = write an adapter; core, fraud, reconciliation, and receipts untouched.
- One coherent answer to "which protocols do we support, at which layer" (the conformance matrix).
- ADR-007/012/013 become the per-layer chapters of one story rather than three separate decisions.
- Trust invariants (kill-switch, attribution, non-repudiation, reconciliation) enforced once and audited once.

**Becomes harder / must be owned**
- The **canonical model + per-layer ports** must be designed and *governed* — and "the core" must be **mechanically delimited**, not tribal: a named module set (`routes/payment_execution_routes.py`, `routes/payment_routing_routes.py`, the settlement/ledger services, `services/agent_checkout_kill_switch.py`) guarded by an automated check (CI grep / import-linter) that rejects `protocol_name` literals outside `services/protocols/`. A review-checklist-only rule erodes.
- A **refactor** to lift today's scattered `protocol_name` checks into the registry — net-reducing but **touching live money paths**, including the fail-closed kill-switch on Rail 1 (`agent_payment_sdk`). A registry lookup that defaults instead of failing closed would silently convert that guard to fail-open; hence the characterization-tests-first sequencing in action item 6.
- A genuinely novel protocol primitive may require a **core-layer extension** (rare, but the price of a neutral spine).
- **Version skew:** each external protocol evolves; adapters must be versioned and the matrix kept current (owner + PR-template trigger, action item 5).

**Must revisit if**
- One protocol wins decisively and others go to ~zero traffic → Option D (or a soft monoculture) may become worth the coupling.
- A new protocol *layer* emerges that none of the four canonical seams model (e.g. native agent-to-agent negotiation) → extend the canonical layer set.

## Action Items

1. [ ] **Founder sign-off** on the meta-model (canonical spine + per-layer edge ports; never fork the core), including the **UCP-is-spine-side** call. Move to Accepted; re-frame ADR-007/012/013 as its instances.
2. [ ] **Name the canonical layers + spine explicitly** in docs — the per-layer ports, the invariant pipeline they wrap, and the **delimited core module set** (the list the automated boundary check in item 7 enforces). Spine of record: `payment_execution_routes` today, + the ADR-013 ledger once built.
3. [ ] **Define the transact-side port interface** (`resolve_identity` / `verify_authorization` / `to_canonical_order` / `select_settlement` / `render_receipt`) and a registry keyed by protocol, homed in `services/protocols/` — **derived from the three existing shapes** (API-key, ACP, AP2), with **unknown-protocol lookups failing closed**.
4. [ ] **ADR-015 — canonical authorized action.** Specify the authorization layer's canonical object (amount, currency, merchant, scope, **presence**) and the mandate / delegated-token / consent-scope mappings onto it. The authorization layer is currently the only canonical layer without an owning ADR, and it carries the most security weight.
5. [ ] **Author the protocol-conformance matrix** (protocol × layer → native / adapted / n-a) beside `docs/AP2_ENABLEMENT.md` — with a **named owner** and an update trigger (the adapter-PR template requires a matrix update), so it cannot silently go stale the way `AP2_ENABLEMENT.md` §6 did.
6. [ ] **Refactor incrementally — characterization tests first.** Before migrating any existing `protocol_name` branching (`agent_payment_sdk` / `payment_execution` / kill-switch) behind the registry, pin the current guard behavior — especially the kill-switch's fail-closed semantics — with characterization tests; then migrate one protocol at a time with no behavior change. **New protocols enter through the registry first; live Rail-1 paths migrate last.**
7. [ ] **Governance rule — mechanical, not tribal:** a new protocol ⇒ a new adapter under `services/protocols/`, mapped to canonical layers, core untouched. Enforce with an automated check (CI grep / import-linter) rejecting `protocol_name` literals in the delimited core module set (item 2); the reviewer checklist is the backstop, not the mechanism.
8. [ ] **Cross-link** ADR-007/012/013 to this ADR as their parent framework.

## Rollback

Framework-level; adds no runtime behavior on its own. Reversing means not building the adapter registry (leave protocol handling as per-protocol code — Option B), a reversible if costlier path. The canonical spine already exists in embryo (`payment_execution_routes`; the ADR-013 ledger joins once built); this ADR **formalizes and de-scatters** it rather than introducing new runtime surface.
