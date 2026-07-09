# Runbook — Tier-2 ACP test-mode capture canary

**Purpose:** prove the P-T2.3.3 backend off-session capture works against **real Stripe test mode** before trusting `AGENT_ACP_TEST_CAPTURE` or wiring the `pivota-acp` `/complete` trigger. Everything below is **test mode** — no real money.

**Why this order:** the off-session capture (`services/acp_offsession_capture.py`) has only been mock-tested. Validate it in **isolation** (Stage 1) via a direct payment call before building the rest of the chain on top of it. Do NOT skip to the full agent flow first.

## Prerequisites
- **PR #1270 deployed** to the target env (merge to `main` and deploy, or deploy the branch to staging). It is the unverified money code — prefer a staging/test deploy first.
- Merchant **`merch_efbc46b4619cfbdf`**: Shopify store connected, **test-mode** Stripe key in `merchant_psps` (live key removed — done). Confirm `merchant_psps.api_key` starts with `sk_test_` (the capture helper hard-refuses `sk_live_`).
- An agent API key that can access `merch_efbc46b4619cfbdf`.

## Canary env flags (set ALL on the target env)
```
AGENT_CHECKOUT_STRICT=on              # default; the guard stays on
SUBMIT_PAYMENT=true                   # opens the ceiling
SUBMIT_PAYMENT_MERCHANTS=merch_efbc46b4619cfbdf   # scope to ONE merchant
AGENT_ACP_TEST_CAPTURE=true           # engage the test-mode capture lane
AGENT_ACP_TEST_MAX_CENTS=500          # $5 hard cap (default; keep low)
# FEATURE_PLATFORM_ORDERS_ACP=true    # only needed for the /checkout/acp session step, not Stage 1
```
**Rollback (instant):** set `AGENT_ACP_TEST_CAPTURE=false` (or `SUBMIT_PAYMENT=false`). Either one closes the lane; no redeploy of code needed.

---

## Stage 1 — validate the off-session capture in isolation (do this FIRST)

Goal: a direct `/agent/v1/payments` call on an ACP-tagged order performs a real **test-mode** off-session capture on `merch_efbc`'s test Stripe, and deposits attribution.

1. **Create an ACP-tagged order** (so the kill-switch + test-capture lane engage and attribution threads):
   ```
   POST /agent/v1/orders/create
   Headers: X-API-Key: <agent key>
   Body: {
     "merchant_id": "merch_efbc46b4619cfbdf",
     "customer_email": "acp-canary@pivota.cc",
     "currency": "USD",
     "items": [{ "merchant_id": "merch_efbc46b4619cfbdf", "product_id": "<real product>",
                 "product_title": "Canary item", "quantity": 1,
                 "unit_price": "1.00", "subtotal": "1.00" }],
     "shipping_address": { ...valid... },
     "metadata": { "protocol_name": "acp", "pvt_click_id": "clk_canary_1" }
   }
   ```
   Note the returned `order_id`. Confirm the order total is **≤ $5** (the cap).

2. **Charge it** (this is the off-session capture):
   ```
   POST /agent/v1/payments
   Headers: X-API-Key: <agent key>
   Body: { "order_id": "<order_id>", "payment_method": { "type": "card" } }
   ```
   With no `payment_method.token`, the helper uses the Stripe test PM `pm_card_visa` (succeeds off-session).

3. **Verify (all must hold):**
   - **HTTP 200**, response `status: "succeeded"`, `psp_used: "stripe"`.
   - **Stripe test dashboard** (merchant's account, *Test mode*): a PaymentIntent for the amount, `off_session`, **succeeded**, metadata `pivota_acp_test_capture=true`.
   - **`payments`** table: a row for the order, `status` succeeded/processing, `psp_type=stripe`.
   - **`commerce_attribution_edges`**: a row for the `order_id` with `click_id` tracing to `clk_canary_1` (P-T2.0 deposit) → GMV rollup picks it up.
   - Backend logs: `ACP TEST-MODE capture lane engaged ... bypassing live PSP readiness` then `acp_offsession: captured ...`.

4. **Negative checks (prove the gates):**
   - Order total **> $5** → `POST /agent/v1/payments` returns **403 `TIER2_TEST_CAPTURE_OVER_CAP`** (no charge).
   - `SUBMIT_PAYMENT_MERCHANTS` set to a *different* merchant → **403 `TIER2_CHARGE_DISABLED`** (`blocked_merchant_not_allowlisted`).
   - `AGENT_ACP_TEST_CAPTURE=false` → the ACP order falls to the normal path and is blocked by live-readiness (test PSP not live-ready) — i.e. the bypass is gone.
   - If `merchant_psps.api_key` were a live key → capture refuses with `live_key_refused` (do not actually test with a live key).

**Known gap:** on success the order currently stays `processing` (no webhook finalizes the off-session capture). That's expected for now — a paid-transition is a tracked follow-up. The capture + attribution are what Stage 1 validates.

**If Stage 1 fails**, stop and fix the backend before touching `pivota-acp`.

---

## Stage 2 — full in-chat chain (only after Stage 1 is green)

Requires the remaining `pivota-acp` work (not yet built): `/complete` → trigger the backend charge, and session-`metadata` persistence so `pvt_click_id` survives. Then:

1. Set `FEATURE_PLATFORM_ORDERS_ACP=true`.
2. Agent calls `POST /agent/v1/checkout/acp` for an ACP-capable, allowlisted merchant → returns an in-chat ACP session (`requires_in_chat_acp_checkout`).
3. Complete the session (buyer path) → `pivota-acp /complete` → backend charge (Stage 1 mechanism) → capture + attribution + GMV, buyer never leaving the agent.
4. Verify the same signals as Stage 1, plus the order/session marked completed.

## Promotion to live (P-T2.3.5) — separate go/no-go
Only after Stage 2 is clean in test mode: swap `merch_efbc`'s Stripe key to **live**, keep `SUBMIT_PAYMENT_MERCHANTS` = that one merchant, keep the cap low, do one watched real charge, then refund. The capture helper's `live_key_refused` guard must be relaxed for the live lane deliberately (a separate change — do not do it implicitly).
