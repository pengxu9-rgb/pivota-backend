# P-T2.3 — Real in-chat ACP charge lane (Option A) — build plan

> ## ⚠️ SUPERSEDED BY ADR-021 (2026-08-01). Historical record — do not build from this.
>
> This plan's architecture is **Option A: make the external `pivota-acp` service charge for real**.
> ADR-021 chose the opposite: **ACP checkout executes IN-PROCESS in pivota-backend**
> (`services/acp_checkout_session_service.py`, table `acp_checkout_sessions`, migration 191), and the
> `pivota-acp` service is retired.
>
> Concretely, in this document:
>
> - **"Mount `platform_orders_acp` router (still `FEATURE_PLATFORM_ORDERS_ACP`-gated)"** is no longer
>   true. That flag and `PLATFORM_ORDERS_ACP_{URL,TOKEN,WEBHOOK_SECRET}` **were removed with their
>   last consumers** — see `config/settings.py:346`. No code reads them. ADR-021 decision 4 is that
>   `PLATFORM_ORDERS_ACP_URL` "stays unset forever".
> - **"`pivota-acp` service = session + capture executor"** describes a retired design. The service
>   still runs on Railway but nothing routes to it: `acp.pivota.cc` is a custom domain on
>   **PIVOTA-Agent**, and no GCP prod service references it.
> - The `Dockerfile.ucp-*` inventory near the end refers to three services **deleted on 2026-08-20**.
>
> **Why this banner exists rather than a quiet edit:** the gated-router line above was read by a
> later audit as evidence that the flag is operative and merely defaults false, and that inference
> reached a partner-facing document before it was caught. A stale plan that still reads as current
> is not harmless — it regenerates the error. The plan is kept for its investigation record (the
> 2026-07-09 finding that `pivota-acp` simulated capture rather than charging is still accurate and
> still worth reading); only its forward architecture is dead.

**Status:** **SUPERSEDED (ADR-021).** Originally: Proposed / kickoff (2026-07-09). Founder decision: **Option A — make `pivota-acp` charge for real** (wire its Stripe adapter + genuine SPT/delegated-payment), not reuse the hosted-checkout path and not integrate a third-party provider.
**Prereqs shipped:** P-T2.0 attribution parity (#1255), P-T2.1 capability resolver (#1256), P-T2.2 fail-closed kill-switch (#1257), P-T2.3a per-merchant canary allowlist (#1259).
**Owner:** Commerce / Fulfillment. **Blast radius:** real money — treat every step as fail-closed + test-mode-first.

## Why this doc exists (the reframing)

The original plan assumed we would "lean on the external provider's SPT/charge." Investigation (2026-07-09) found that assumption is false:

- **`pivota-acp` is Pivota's OWN microservice** (`github.com/pengxu9-rgb/pivota-acp`, live code `pivota_infra/src/`), not a third-party ACP provider. It implements the ACP `checkout_sessions` spec itself.
- **It does not charge.** On `POST /checkout_sessions/{id}/complete` the Shopify connector runs `del payment_reference` (discards the token) then creates a Pivota order and **simulates** capture via a fake `payment_captured` webhook to the backend. The real Stripe code (`src/mcp/stripe_adapter.py`) is unwired.
- **SPT/delegated-payment exists only as scaffolding in `pivota-acp`** — `POST /agentic_commerce/delegate_payment` mints a `vt_` vault token and `/complete` enforces the allowance (amount/currency/session match) — but then discards it without charging. The **backend has zero SPT plumbing**.
- The backend ACP router (`routes/platform_orders_acp.py`) is **not mounted**, feature-flagged off, merchant/admin-auth only, **no agent-facing entry point**; `platform_orders` rows are created only by the catalog import worker.

So Option A is genuinely greenfield real-money work spanning **two services**. This doc sequences it behind the fail-closed switch we already built.

## Hard open questions — MUST be answered before any charge code (P-T2.3.2+)

1. **Whose funds does `pivota-acp` capture against?** For merchant-of-record + post-hoc GMV (today's topology, memory `agent-checkout-money-topology`), the capture must hit the **merchant's** PSP, not Pivota's. Options: (a) Stripe Connect `destination`/`on_behalf_of` with `application_fee` (this WOULD be at-source take — a topology change the plan deferred), or (b) capture directly on the merchant's own connected Stripe key (`merchant_psps`), Pivota takes post-hoc. **Decision needed** — this shapes the whole adapter.
2. **PCI / tokenization boundary.** `delegate_payment` currently accepts a raw `PaymentMethodCard`. For PCI-DSS we must NOT let raw PAN reach our servers: the **buyer surface tokenizes via Stripe.js** → `pm_`/`SetupIntent`, and `delegate_payment` receives only a token + a Pivota-minted allowance. **Decision needed** — confirm the tokenization surface and that we never persist PAN.
3. **Delegated-payment authority.** The allowance (max_amount, currency, session, expiry) is the spend mandate. Decision: the **backend** is the authority — it mints the allowance ONLY when `evaluate_tier2_charge` allows, and `pivota-acp` refuses to capture without a backend-signed allowance. (Recommended; encoded below.)

## Target flow (Option A)

```
agent → find_products_multi → decision layer resolves capability (P-T2.1: protocols=["acp"])
      → kill-switch allows (P-T2.2/2.3a: strict ON + SUBMIT_PAYMENT + merchant on allowlist)
      → backend mints a delegated-payment ALLOWANCE (spend mandate, signed) ── the gate lives HERE
      → agent renders in-chat ACP checkout screen (pivota-acp session; cart/quote/fulfillment)
      → buyer tokenizes card on buyer surface (Stripe.js → pm_); confirms
      → pivota-acp /complete: validate allowance → CAPTURE for real via Stripe adapter
           against the MERCHANT's PSP (merchant of record)   ← the new money movement
      → order-completed webhook → backend deposits attribution edge (P-T2.0) + GMV
```

The through-line invariant (all tiers): a paid order deposits a `commerce_attribution_edge` with `pvt_click_id`. Already true for this lane via P-T2.0.

## Two-service responsibilities

- **backend (`pivota-backend`)** = policy + identity + attribution authority.
  - Agent-authenticated entry point that (a) resolves capability, (b) runs `evaluate_tier2_charge(merchant_id, protocol="acp")`, (c) on allow, mints a signed **allowance** and initiates the `pivota-acp` checkout session, (d) returns the in-chat checkout descriptor to the agent.
  - Mount `platform_orders_acp` router (still `FEATURE_PLATFORM_ORDERS_ACP`-gated).
  - Receive the order-completed webhook → attribution edge + GMV (done).
  - NEVER sees raw PAN.
- **pivota-acp service** = session + capture executor.
  - Wire `stripe_adapter` into `/complete` to capture for real against the merchant PSP.
  - Refuse to capture without a valid backend-signed allowance (defense-in-depth env guard mirroring `SUBMIT_PAYMENT`).
  - Idempotent capture; emit the completed webhook the backend already consumes.

## Phased sub-plan (each ships behind flags; nothing charges until .4)

### P-T2.3.1 — backend: agent-facing ACP initiation, DARK (no charge) [M]
Mount `platform_orders_acp` (flagged off). Add an agent-authenticated entry point that resolves capability (P-T2.1), evaluates the kill-switch (P-T2.2/2.3a), and — when allowed — creates a `pivota-acp` checkout session and returns an in-chat checkout descriptor. Thread `pvt_click_id`. **No capture** (pivota-acp still simulates). Kill-switch gates initiation so a blocked merchant can't even start. Verify: an agent call for an allowlisted test merchant returns a session; a non-allowlisted merchant is refused; attribution still deposits on the simulated completion.

### P-T2.3.2 — pivota-acp: real Stripe capture against merchant PSP, TEST MODE [L]
In `pivota-acp`, wire `stripe_adapter` into `/complete` to capture against the **merchant's** PSP (per open-question #1). Enforce the allowance (amount/currency/session/expiry) and idempotency; stop discarding the token. Behind its own `ACP_ENABLE_REAL_CAPTURE` env (default off) + require a backend-signed allowance. Test-mode Stripe keys only. Verify: a test-mode capture succeeds against a test connected account; no-allowance and over-allowance are refused.

### P-T2.3.3 — SPT/delegated-payment end-to-end, TEST MODE [L]
Backend mints the signed allowance (gated by `evaluate_tier2_charge`); buyer surface tokenizes via Stripe.js; agent carries `vt_`/`pm_` to `/complete`. Full delegated-payment loop in test mode. Verify: allowance minted only when kill-switch allows; token never touches PAN on our servers; end-to-end test-mode order → capture → completed webhook → attribution edge + GMV.

### P-T2.3.4 — test-mode canary, ONE merchant [M]
`AGENT_CHECKOUT_STRICT=on` + `SUBMIT_PAYMENT=true` + `SUBMIT_PAYMENT_MERCHANTS=<test merchant>` + `ACP_ENABLE_REAL_CAPTURE=on` (test keys). Drive one real in-chat ACP checkout on the Pivota test Shopify store end-to-end. Verify the P-T2.3 acceptance: paid (test) order, buyer never left the agent surface, attribution edge + GMV row present.

### P-T2.3.5 — live canary, ONE merchant, watched [M/decision]
Swap to live keys for exactly one real merchant (allowlist). Real money, low volume, monitored, one-flip rollback. Only after .4 is clean. Separate go/no-go.

## Safety architecture (non-negotiable)

- **Fail-closed everywhere.** Backend kill-switch (strict ON default, `SUBMIT_PAYMENT` OFF, per-merchant allowlist) gates allowance minting. pivota-acp refuses capture without a backend-signed allowance AND its own real-capture env being on. Two independent gates, both default-closed.
- **Capability fail-open to redirect.** If capability mis-resolves or the ACP lane errors, fall back to the live redirect floor — never a dead end (plan risk #3).
- **No PAN on our servers.** Tokenize on the buyer surface; only tokens + allowances cross service boundaries.
- **Idempotent capture** keyed on the checkout session / order; at-least-once webhooks must not double-charge or double-count GMV.
- **Test-mode before live**, one merchant before many.

## External inputs required (blocking .2+)
- Decision on open-questions #1 (capture target: merchant connected Stripe vs Connect+application_fee) and #2 (tokenization surface).
- Test-mode Stripe keys for `pivota-acp` + a test merchant with a test-mode PSP in `merchant_psps`.
- A Pivota test Shopify store connected as that merchant.
- Confirmation to modify the `pivota-acp` repo (`github.com/pengxu9-rgb/pivota-acp`; local `/Users/pengchydan/dev/pivota-acp-revert`).

## Explicitly NOT doing (v1)
At-source take split unless open-question #1 forces Connect+application_fee (would be a topology change, flag separately); UCP/AP2 (P-T2.4); un-pausing T7; any live charge before .4 is clean.

## Acceptance (P-T2.3 done)
One real (test-mode first, then one live merchant) in-chat ACP checkout on the Pivota test Shopify store → captured order against the merchant PSP → `commerce_attribution_edge` with `pvt_click_id` + GMV row → buyer never left the agent surface; every gate fail-closed and one-flip reversible.
