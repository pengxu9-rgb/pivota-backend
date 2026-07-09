# P-T2.3.4 — pivota-acp `/complete` → backend charge wiring (spec)

**Status:** Spec (2026-07-09). Ready to implement **in a pivota-acp dev env** (the change is in `github.com/pengxu9-rgb/pivota-acp`, local `/dev/pivota-acp-revert`, `pivota_infra/src/`). Written from the **proven** P-T2.3.3 canary (backend off-session capture succeeded on real Stripe test mode: order `ORD_514BCE3BC16F52CD`, PaymentIntent `pi_3TrFddKBoATcx2vH1lj5iDrV`).

**Why a spec, not code:** pivota-acp is not testable from the backend author's sandbox (no deps/venv/tests; deploy blocked). Blind edits to a payments service are unsafe. Implement + test this where pivota-acp runs.

## Architecture (decided)
BACKEND charges; pivota-acp **triggers**. On `/complete`, pivota-acp drives the same 3 backend calls the canary proved — **quote → order(acp) → pay** — instead of the simulated `payment_captured` webhook. All behind a **default-off** flag so current behavior is unchanged until enabled.

## Flag
`ACP_ENABLE_REAL_CAPTURE` (env, default `false`). When false → keep today's simulation path (`connectors/shopify.create_order` step 2). When true → real backend capture (below).

## The change — `pivota_infra/src/connectors/shopify.py::create_order`
Replace the simulated capture (the `POST {backend}/api/payments/webhooks/checkout {payment_captured}` block) with, when `ACP_ENABLE_REAL_CAPTURE` is on, the backend agent flow. `base_url = PIVOTA_BACKEND_BASE_URL`, `headers = {"X-API-Key": PIVOTA_AGENT_API_KEY}` (both already read here).

Exact calls (verbatim shapes from the working canary):

1. **Quote** — `POST {base}/agent/v1/quotes/preview`
   ```json
   {"merchant_id": "<mid>",
    "items": [{"product_id":"<PID>","variant_id":"<VID>","quantity":<q>}],
    "shipping_address": <SHIP>}
   ```
   → read `quote_id`. **`shipping_address` MUST be included** and byte-match the order's (the order path fingerprints `items + discount_codes + shipping_geo + delivery`; a missing/differing address → `QUOTE_MISMATCH`).

2. **Order** — `POST {base}/agent/v1/orders/create`
   ```json
   {"merchant_id":"<mid>","quote_id":"<quote_id>","customer_email":"...","currency":"<cur>",
    "items":[{"merchant_id":"<mid>","product_id":"<PID>","product_title":"...","variant_id":"<VID>",
              "quantity":<q>,"unit_price":"<price>","subtotal":"<price*q>"}],
    "shipping_address": <SHIP>,
    "metadata": {"protocol_name":"acp", "pvt_click_id":"<click id if available>",
                 "checkout_session_id":"<session id>"}}
   ```
   → read `order_id`. **`metadata.protocol_name="acp"` is REQUIRED** — it's what engages the kill-switch + test-capture + attribution gates. Same items + same `shipping_address` as the quote.

3. **Pay (off-session capture)** — `POST {base}/agent/v1/payments`
   ```json
   {"order_id":"<order_id>","payment_method":{"type":"card"}}
   ```
   → expect `{"status":"succeeded","psp_used":"stripe",...}`. The backend does the off-session capture on the merchant's Stripe (P-T2.3.3) and transitions the order to paid + GMV (P-T2.3.3b).

Then keep the existing "3) create-shopify" step for fulfillment; return the `OrderRef` as today.

**Do these 3 calls back-to-back with no delay** — quotes expire in minutes (canary hit `QUOTE_NOT_FOUND` on a slow gap). On any non-2xx, do NOT fall back to simulation when the flag is on — fail honestly (log + raise) so a real-capture error surfaces.

## The two hard sub-problems (must solve in the dev env)

### 1. ACP line-item → backend `product_id` / `variant_id`
The ACP session stores `items` as `{id, quantity}` where `id` was set by the backend session client to `sku || variant_id || product_id`. The backend quote needs a real `product_id` **and** `variant_id`. Options, in preference order:
- **Best:** carry `product_id` + `variant_id` explicitly through the session (extend the ACP item model + the backend `pivota_acp_client._acp_items` to send both, and persist them). Then map 1:1.
- **Fallback:** resolve `id` → (product_id, variant_id) via a backend lookup (`find_products` / a resolve endpoint) at `/complete`.

### 2. `pvt_click_id` (attribution) — needs the session to carry metadata
The session table has **no `metadata` column** and `CheckoutSessionCreateRequest` has **no `metadata` field**, so the `pvt_click_id` the backend sends at session-create is dropped. To thread it:
- Add `metadata` to `CheckoutSessionCreateRequest` (models.py), persist it (ALTER `checkout_sessions` ADD COLUMN `metadata JSONB` + add to `create_session` INSERT + it flows back via `get_session`'s `SELECT *`), read it at `/complete`, pass `metadata.pvt_click_id` into the order.
- **Until then:** the capture still works (gates key off `protocol_name`, not the click id); the attribution edge is just unattributed (`click_matched=false`). Acceptable for v1 — note it.

## Verification (in a deployed test-mode env)
1. `ACP_ENABLE_REAL_CAPTURE=true` on pivota-acp; backend armed for the canary merchant (`SUBMIT_PAYMENT=true` + `SUBMIT_PAYMENT_MERCHANTS=<merchant>` + `AGENT_ACP_TEST_CAPTURE=true`, test Stripe key).
2. Drive the full agent flow: backend `POST /agent/v1/checkout/acp` → in-chat session → pivota-acp `/complete` → backend capture.
3. Confirm: Stripe **test** PaymentIntent succeeded ≤ cap; order `paid`; `commerce_attribution_edges` row (attributed once `pvt_click_id` threads).

## Not doing here
Live capture (P-T2.3.5 — relax the `live_key_refused` guard deliberately); UCP/AP2. Default-off means nothing changes until `ACP_ENABLE_REAL_CAPTURE` is flipped.
