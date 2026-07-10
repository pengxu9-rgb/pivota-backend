# UCP in-chat capture canary runbook (P-T2.4)

Proves the **UCP** in-chat off-session capture end-to-end on real Stripe **test**
mode — the UCP analog of the Wix/Stripe canary
([wix_acp_test_capture_canary.md](./wix_acp_test_capture_canary.md)). The capture
reuses the proven backend `quote → order(ucp) → pay` off-session flow, driven by
the **UCP web app** (`pivota_infra_main`, PR pivota-acp #30).

> **Who runs it:** the operator. The auto-mode classifier blocks the agent from
> running the charge; Stripe test-dashboard verification is yours.

## How UCP differs from the Wix/Adyen canary (read first)

1. **Line items are signed offer tokens, not `product_id`/`variant_id`.** A UCP
   `line_items[].item.id` is an `offer_v1` token that *embeds* merchant_id +
   product_id + variant_id + price. You must **mint one first** (Step 3). It **must
   carry a `variant_id`** — the capture re-verifies the offer and escalates if the
   variant is missing (the backend quote is keyed on product_id + variant_id).
2. **The UCP web app drives the capture directly** — there is no separate pivota-acp
   connector. Endpoints live under `/ucp/v1` on the **UCP web service**
   (`ucp_web:app`), not `api.pivota.cc`.
3. **Dispatch is by the merchant's active PSP** (test Stripe) at the backend — same
   as Wix (`capture_offsession` resolves the merchant's runtime PSP).
4. **`UCP-Agent` header** is required at session create (or set
   `UCP_ALLOW_MISSING_UCP_AGENT=true`).
5. **Test lane** → no buyer token (backend test PM). Live buyer-token intake is a
   deferred follow-up (see Known limitations).

## Prerequisites

- A merchant `<UCP_MERCHANT_ID>` with an **active test Stripe PSP** and a
  **backend-quotable** product (a real `product_id` + `variant_id` the backend
  `/agent/v1/quotes/preview` can price — e.g. a Winona-style test SKU). *(The old
  merch_efbc test rig was retired in #1332 — use a merchant that still has a test
  Stripe PSP + a quotable product.)*
- The **UCP web service** deployed on current `main` (has #30). Note its base URL
  `<UCP_WEB_URL>` (the Railway service serving `/ucp/v1/...`).
- `UCP_OFFER_TOKEN_SECRET` set on the UCP web service (mint + verify must use the
  same secret, consistent across replicas).

## 1. Arm the BACKEND (Railway `web` env)

Same test-capture canary flags as the ACP lane (the UCP order is `protocol_name=ucp`
→ the backend routes it through the identical off-session capture):
```
AGENT_CHECKOUT_STRICT=on
SUBMIT_PAYMENT=true
SUBMIT_PAYMENT_MERCHANTS=<UCP_MERCHANT_ID>
AGENT_ACP_TEST_CAPTURE=true
AGENT_ACP_TEST_MAX_CENTS=2000
```
(`FEATURE_PLATFORM_ORDERS_ACP` is **not** needed — that gates the ACP router; UCP
doesn't use it.) Leave all live flags OFF.

## 2. Arm the UCP WEB service (Railway env)

```
UCP_ENABLE_REAL_CAPTURE=true
PIVOTA_BACKEND_BASE_URL=https://api.pivota.cc
PIVOTA_AGENT_API_KEY=<working ak_ agent key>
UCP_OFFER_TOKEN_SECRET=<mint/verify secret>
UCP_INTERNAL_OFFER_MINT_KEY=<a secret you choose for minting>
UCP_ALLOW_MISSING_UCP_AGENT=true            # or send a real UCP-Agent header at create
```

## 3. Mint a signed offer for the quotable product

`POST <UCP_WEB_URL>/internal/ucp/mint-offer` (auth via `X-Pivota-Internal-Key`).
**Include a `variant_id`** and use the real backend-quotable product ids:
```bash
curl -sS -X POST "<UCP_WEB_URL>/internal/ucp/mint-offer" \
  -H "X-Pivota-Internal-Key: <UCP_INTERNAL_OFFER_MINT_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"merchant_id":"<UCP_MERCHANT_ID>","product_id":"<PID>","variant_id":"<VID>",
       "currency":"USD","price_minor":169,"title":"Canary SKU","exp_seconds":3600}'
```
Record the returned **`offer_id`** (it expires — do Step 4 promptly).

## 4. Run the chain (on the UCP web service)

**4(a) — create the checkout session:**
```bash
curl -sS -X POST "<UCP_WEB_URL>/ucp/v1/checkout-sessions" \
  -H "UCP-Agent: profile=\"https://example.com/agent\"" \
  -H "Content-Type: application/json" \
  -d '{"line_items":[{"item":{"id":"<offer_id>"},"quantity":1}],"currency":"USD"}'
```
Expect a `checkout_id` (`ucp_chk_...`) + `continue_url` + `status: requires_escalation`
(create never charges).

**4(b) — complete → in-chat capture:**
```bash
curl -sS -X POST "<UCP_WEB_URL>/ucp/v1/checkout-sessions/<checkout_id>/complete"
```
With `UCP_ENABLE_REAL_CAPTURE` on, this re-verifies the offer → drives
`quote → order(ucp) → pay` on the backend → off-session capture on the merchant's
test Stripe → links the order. Expect `status: completed` with an `order` block
(`order.id = ORD_...`). If it returns `requires_escalation`, the capture fell back
(check: flag on? backend creds set? offer has a variant_id? amount ≤ cap?).

## 5. Verify

- **Backend order**: `GET https://api.pivota.cc/agent/v1/orders/<order_id>`
  (add `--resolve api.pivota.cc:443:69.46.46.126` if your network can't reach the
  edge) → `payment_status=paid`, merchant `<UCP_MERCHANT_ID>`.
- **Stripe test dashboard**: a succeeded off-session PaymentIntent (~$1.69 — the
  backend re-quote of the item; the UCP session totals are cosmetic placeholders).
- **UCP session**: `GET <UCP_WEB_URL>/ucp/v1/checkout-sessions/<checkout_id>` →
  `status: completed`, `order_id` set.

## 6. Disarm (immediately after)

Backend: `AGENT_ACP_TEST_CAPTURE=false`, `SUBMIT_PAYMENT=false`.
UCP web: `UCP_ENABLE_REAL_CAPTURE=false`.
Re-run 4(a)+4(b) on a fresh session → 4(b) returns `requires_escalation` (dark).

## Known limitations (v1 — deferred follow-ups)

- **Live buyer-token intake (Q1).** The test lane uses the backend test PM
  (`payment_token=None`). A real card charge needs the UCP buyer-token model — the
  one genuinely-new design piece, not yet built.
- **Real buyer shipping address.** UCP collects none at create, so the capture uses
  a consistent placeholder (fine for the test canary; the amount comes from the
  backend re-quote).
- **Platform order webhook.** The capture path links the backend order + completes
  but does **not** fire the platform's order webhook — the existing `_link-order`
  webhook needs a UCP-**local** order row, and the backend order lives in the
  backend DB. Wiring the webhook off the backend order is a follow-up.

## Promote to LIVE (later)

Mirrors the ACP live lane: solve buyer-token intake, use a live merchant PSP + a
real buyer card token, and arm `AGENT_ACP_ALLOW_LIVE_CAPTURE` +
`AGENT_ACP_LIVE_CAPTURE_MERCHANTS` (JSON array) + `AGENT_ACP_LIVE_MAX_CENTS` on the
backend (the UCP order flows through the same live-capture lane as ACP).
