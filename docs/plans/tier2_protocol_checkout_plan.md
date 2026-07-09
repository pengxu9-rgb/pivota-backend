# Tier-2 in-chat protocol checkout — build plan (ACP/UCP/AP2)

**Status:** Proposed / kickoff (2026-07-09). Prereq context: the redirect funnel (Tier 0/1) is live + canary-ready; this is the next major build.
**Owner:** Commerce-index / Fulfillment
**Related:** ADR-009 (seller-of-record), ADR-010 (canonical identity), `docs/adr/` protocol notes, memory `protocol-integration-architecture`, `redirect-commission-loop-status`, `pivota-convergence-plan`.

## Context — why this is the key focus

Founder direction: **most agent traffic will complete purchase *in-chat* via agentic-commerce protocols (ACP / UCP / AP2), not via redirect.** In that model Pivota routes agent traffic to a merchant's own checkout **backend/PSP** without the merchant integrating with Pivota — the agent renders the checkout *screen*, the merchant's rails process the *transaction*, the merchant stays merchant-of-record, Pivota takes GMV post-hoc.

Two mechanisms, do not conflate:
- **Redirect (Tier 0/1, BUILT + live):** buyer *leaves* the chatbox → merchant's own checkout *page*; attribution via `pvt_click_id` round-trip. Works on any storefront, zero merchant capability required. This is the floor.
- **Protocol checkout (Tier 2, THIS PLAN):** buyer *stays* in the chatbox → agent-rendered UI → order submitted to the merchant **programmatically** via the protocol (delegated payment / shared payment token); merchant's PSP captures. Requires the merchant's **platform/PSP to support the protocol** (the capability gate) — not a Pivota integration, but not zero-integration either.

## Verified current state (must re-verify at kickoff — code moves)

- **Only real charge path is protocol-agnostic:** `routes/agent_payment_sdk.py::create_payment` (`/agent/v1/payments`) → `psp_adapter`/`multi_psp_orchestrator`/`psp_payment_finalizer`. Reuse this as the settlement primitive.
- **ACP = validation stub + external HTTP proxy** (`routes/platform_orders_acp.py`, gated `FEATURE_PLATFORM_ORDERS_ACP=false`, `config/settings.py:327`). **UCP = absent** (readiness export only). **AP2 = dormant** (router unmounted; `adapters/ap2_payment_adapter.py` never instantiated; consent/signature TODO). **MCP = real OAuth AS + simulated commerce.**
- **No SPT / delegated-payment handling anywhere** (grep-clean outside tests) — the genuinely new, hard piece.
- **Attribution kernel is real + live** on order/payment paths: `services/commerce_attribution_service.py` (`materialize_attribution_context:81`, `upsert_order_attribution_edge:312`, `pvt_click_id`) — but **ZERO protocol paths call it** ("zero attribution on ACP/MCP"). This is the cheap, high-leverage first fix.
- **Kill-switches `AGENT_CHECKOUT_STRICT` / `SUBMIT_PAYMENT` are DOC-ONLY** (no code reads them) — a real gate must be implemented.
- **Capability signal**: `merchant_commerce_readiness_state{primary_platform, active_psp}` resolver exists (connected-only); `services/platform_capabilities.py` static matrix has NO protocol dimension; no domain fingerprinting for crawl merchants; the only protocol signal is an ephemeral Shopify-only heuristic (`readiness/sources/shopify_live.py`).
- **Money topology:** merchant is merchant-of-record; Pivota take is post-hoc GMV invoice (T6 rollup live, default 10%; T7 paused correctly). No at-source split (no Stripe Connect application_fee).
- **Founder decisions locked:** ACP Tier-2 via **external provider integration** (not in-house); index-first sequencing overall; take stays post-hoc for v1.

## Target architecture

```
agent → find_products_multi (index) → decision layer picks offer + resolves CAPABILITY
      → if merchant rails speak ACP/UCP/AP2 → IN-CHAT protocol checkout
           (agent-rendered UI; order submitted to merchant backend via protocol;
            merchant PSP captures; pvt_click_id threaded into order/payment metadata)
      → else → redirect (Tier 0/1 floor, already live)
      → attribution edge (same rail, both tiers) → GMV → T7
```

The through-line across all tiers is **`pvt_click_id`**: it rides cart-attributes (Tier-1 Shopify), `utm_content` (Tier-1 Woo), and must ride the **protocol order/payment metadata** (Tier-2). Attribution parity is the invariant.

## Phases (all gated; each ships value alone)

### P-T2.0 — Attribution parity on protocol paths (do FIRST; cheapest, unblocks measurement)
Wire the existing kernel into every protocol/checkout path so a Tier-2 transaction deposits a `commerce_attribution_edge` with `pvt_click_id`, exactly like redirect. Touch: the ACP proxy (`routes/platform_orders_acp.py` — carry click id/surface into its metadata), `_handle_submit_payment` (`routes/agent_shop_gateway.py`), any AP2/MCP order path that could go live. Reuse `materialize_attribution_context` + `upsert_order_attribution_edge`. **No new checkout capability — just makes future protocol orders measurable.** [M]
- Verify: a simulated protocol order lands an edge with the threaded click id.

### P-T2.1 — Capability resolver (protocols[] dimension + crawl fingerprinting)
`resolve_merchant_capability(merchant_id) -> {platform, psp, protocols[]}`. Extend `services/platform_capabilities.py` with a real per-(platform, PSP) protocol matrix; extend `merchant_commerce_readiness_state` (or a companion) to persist `protocols[]`; add domain-based platform fingerprinting so **crawled** merchants resolve a platform (today they're None/placeholder). This is the gate that routes traffic to Tier-2 vs redirect. [L]
- Verify: resolver returns a truthful `protocols[]` for the connected Shopify test merchants; crawl merchant resolves a platform (or honest "unknown").

### P-T2.2 — Real kill-switch + strict gate (safety before any charge)
Implement the doc-only `AGENT_CHECKOUT_STRICT` / `SUBMIT_PAYMENT` as actual code gates on the charge path; default fail-closed. Prereq to raising the ceiling. [S]

### P-T2.3 — One real ACP-provider lane (the ceiling, external provider per founder decision)
Integrate the external ACP provider end-to-end behind the capability gate + strict switch: agent-initiated checkout → provider → merchant backend/PSP (merchant of record) → order + `pvt_click_id` in-band (from P-T2.0). Delegated-payment/SPT handling is the hard new part — scope whether the provider supplies it. Start with the ONE densest pairing (Shopify + ACP). [L]
- Verify: one real (test-mode) in-chat ACP checkout on the Pivota test Shopify store → paid order → attribution edge + GMV row, buyer never leaving the agent surface.

### P-T2.4 — Extend + settle
UCP/AP2 lanes as demand appears (same pattern); decide take model (keep post-hoc GMV invoice for v1 vs at-source via delegated payment — a real money-topology + compliance decision, defer). [L/decision]

## Sequencing & dependencies
- **P-T2.0 first** (cheap, no capability needed, makes everything measurable) — can start immediately, parallel to the redirect canary.
- P-T2.1 (resolver) gates P-T2.3 routing. P-T2.2 (kill-switch) gates any real charge. P-T2.3 needs 2.0+2.1+2.2.
- Independent of the redirect canary (different code paths) — Tier-2 does not block, and is not blocked by, first-dollar M1.

## Top risks
1. **Real-money in-chat charge** — fail-closed kill-switch (P-T2.2) is a hard prereq; canary in Shopify test mode first (as with redirect).
2. **Delegated payment / SPT is greenfield** — de-risk by leaning on the external provider's SPT handling (founder's integrate-not-build decision); do not build SPT in-house for v1.
3. **Capability mis-resolution routes traffic to a protocol a merchant can't honor** → fail-open to the redirect floor (never a dead end); resolver defaults conservative.
4. **Attribution silently absent on a live protocol path** (today's state) → P-T2.0 lands before any Tier-2 traffic so nothing transacts un-attributed.

## Explicitly NOT doing
In-house ACP/UCP/AP2 protocol implementation (external provider); SPT/delegated-payment built in-house; at-source take split (post-hoc GMV invoice stays for v1); MCP commerce realization; un-pausing T7 (separate track, gated on real accrual); any Tier-2 charge before the real kill-switch exists.

## Verification (per phase, above) + standing
Standing: every tier deposits an attribution edge with `pvt_click_id` (parity invariant); capability resolver re-run as merchants connect; Tier-2 charges gated behind strict switch + test-mode canary before live.
