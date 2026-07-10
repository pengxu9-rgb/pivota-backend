# Wix ACP test-capture canary runbook (P-T2.3.6)

Proves the **Wix** in-chat ACP money path end-to-end on real Stripe **test mode**,
exactly as the Shopify canary did for `merch_efbc46b4619cfbdf`. When this passes,
promote the Wix store to `order_writeback_status='enabled'` to light up the
production lane (PR #1314, dark until then).

> **Who runs it:** the operator (you). The auto-mode classifier blocks the agent
> from running prod charge/order calls, and Stripe test-dashboard verification is
> yours. The agent prepared this recipe; it does not execute steps 3–4.

## Why Wix is ready to canary

- **Connector**: pivota-acp `WixConnector` is registered in the ACP router and
  drives the *same* shared flow as Shopify —
  `connectors/_real_capture.real_capture_via_backend` (quote → order(acp) → pay),
  with token-forwarding and the session's real fulfillment address/metadata
  threaded. (pivota-acp #27)
- **Backend gate**: `_ACP_TEST_CAPABLE_PLATFORMS = {"shopify", "wix"}` in
  `services/tier2_acp_lane.py`, so an allowlisted Wix merchant with
  `AGENT_ACP_TEST_CAPTURE=true` routes through the off-session **test** lane
  without needing a live PSP. (pivota-backend #1306)
- **Platform routing is automatic**: the backend resolves the merchant's platform
  as `wix` (connected store) → `POST /agent/v1/checkout/acp` returns
  `lane=acp_in_chat` → the backend ACP client sends `X-Platform: wix` to
  pivota-acp → the `WixConnector` is selected at session create.

## ⚠️ Multi-store merchants — target the Wix store explicitly

The canary merchant **merch_efbc46b4619cfbdf has BOTH a Shopify and a Wix store
connected.** The ACP lane resolves a merchant to ONE platform via its **primary**
store, so without targeting, the "Wix canary" silently re-runs the **Shopify**
connector. Two ways to point it at Wix:

- **Preferred — store/platform override** (backend PR #1316): pass
  `"platform": "wix"` (or `"store_id": "<wix_store_id>"`) in the 3(a) request
  body. No DB mutation; the lane resolves capability against the Wix store and
  sends `X-Platform: wix` to pivota-acp. Requires #1316 deployed.
- **Fallback — flip `is_primary`** (reversible):
  `UPDATE merchant_stores SET is_primary=(platform='wix') WHERE
  merchant_id='merch_efbc46b4619cfbdf';`

Confirm current state first:
```sql
SELECT store_id, platform, status, is_primary, connected_at, order_writeback_status,
       (api_key IS NOT NULL AND api_key <> '') AS has_api_key, domain
FROM merchant_stores WHERE merchant_id='merch_efbc46b4619cfbdf'
ORDER BY is_primary DESC, connected_at DESC;
```

## Prerequisites

- A **Wix** store connected to the canary merchant in prod (`<WIX_MERCHANT_ID>`),
  `status='active'`, resolvable as `platform='wix'`.
- That merchant has a **test-mode Stripe** key in `merchant_psps` (the Wix analog
  of what `merch_efbc` has). Off-session test capture uses this key; **do not**
  set a live key for the test canary.
- Both services deployed on current `main` (backend ≥ `#1313`, pivota-acp ≥ `#27`).

## 1. Arm the BACKEND (Railway `web` service env)

```
AGENT_CHECKOUT_STRICT=on
SUBMIT_PAYMENT=true
SUBMIT_PAYMENT_MERCHANTS=<WIX_MERCHANT_ID>
AGENT_ACP_TEST_CAPTURE=true
AGENT_ACP_TEST_MAX_CENTS=2000          # bump if real Wix shipping pushes over the cap
FEATURE_PLATFORM_ORDERS_ACP=true
```

Leave all **live**-capture flags OFF: `AGENT_ACP_ALLOW_LIVE_CAPTURE` unset/false.

## 2. Arm the pivota-acp SERVICE (Railway env)

```
ACP_ENABLE_REAL_CAPTURE=true
PIVOTA_MERCHANT_ID=<WIX_MERCHANT_ID>   # GOTCHA: the connector charges THIS merchant, not the session's
PIVOTA_BACKEND_BASE_URL=https://api.pivota.cc
PIVOTA_AGENT_API_KEY=<working ak_live_ agent key>   # must authenticate backend /quotes,/orders,/payments
```

Confirm the ACP app (`src.main:app`, serves `/checkout_sessions`) is the deployed
image at `pivota-acp-production.up.railway.app` — a direct probe to
`POST /checkout_sessions` should 201 with a `csn_...` id. (If it 404s, the service
reverted to the UCP image; see the P-T2.3.4 notes on forcing `railway.json`.)

## 3. Run the two-call chain

**3(a) — backend renders the in-chat ACP session** (agent-authed):

```bash
curl -sS -X POST https://api.pivota.cc/agent/v1/checkout/acp \
  -H "X-API-Key: <working ak_live_ agent key>" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [{"merchant_id": "<WIX_MERCHANT_ID>", "product_id": "<PID>", "variant_id": "<VID>", "quantity": 1}],
    "platform": "wix",
    "shipping_address": {
      "name": "Test Buyer", "address_line1": "1 Test St",
      "city": "San Francisco", "state": "CA", "postal_code": "94105", "country": "US"
    },
    "pvt_click_id": "clk_wix_canary_1", "pvt_surface": "canary"
  }'
```

`"platform": "wix"` (PR #1316) targets the Wix store on this multi-store merchant.
Expect `status: requires_in_chat_acp_checkout`, `lane: acp_in_chat`,
`acp_session_id: csn_...`, `platform: wix`, and `capability.store_selector.matched:
true`. If `lane` is `redirect_floor`, the canary gate isn't armed — recheck step 1
(`SUBMIT_PAYMENT_MERCHANTS` must contain `<WIX_MERCHANT_ID>`) and that the override
matched a Wix store (`store_selector.matched`).

**3(b) — complete on pivota-acp → triggers the backend off-session capture:**

```bash
curl -sS -X POST \
  'https://pivota-acp-production.up.railway.app/checkout_sessions/<csn_...>/complete' \
  -H 'Authorization: Bearer <PLATFORM_ORDERS_ACP_TOKEN>' \
  -H 'API-Version: 2025-09-29' \
  -H 'X-Merchant-Id: <WIX_MERCHANT_ID>' \
  -H 'X-Platform: wix' \
  -H 'Content-Type: application/json' \
  -d '{"payment_data": {"provider": "stripe"}}'
```

- Only `payment_data.provider` is required. **Omit** `payment_data.token` for the
  test canary — with no token the connector lets the backend fall back to its test
  PM (`pm_card_visa`). (A `tok_test` string would break the paired test-lane guard;
  token-forwarding is for the live lane only.)
- `X-Platform: wix` forces the `WixConnector` at `/complete`.

This drives `WixConnector.create_order` → `real_capture_via_backend` →
backend `quote → order(protocol=acp) → payments` off-session capture on the
merchant's **test** Stripe key.

## 4. Verify

- **Backend order**: `GET /agent/v1/orders/{order_id}` (order_id from the /complete
  response permalink) → `payment_status=paid`, merchant `<WIX_MERCHANT_ID>`, and a
  Wix `platform_order_id` / `wix_order_id` on the OrderRef.
- **Stripe test dashboard** (merchant's test account): a succeeded off-session
  PaymentIntent for the item amount.
- **Attribution**: the order carries `pvt_click_id=clk_wix_canary_1`
  (protocol_name `acp`) → an attributed edge deposits (P-T2.0 parity).

> Pricing note (harmless): the ACP-session total is placeholder pricing; the
> **backend** re-quotes the real amount for the capture. Expect the Stripe PI to
> match the backend quote, not the session total (same cosmetic mismatch seen in
> the Shopify canary).

## 5. Disarm (immediately after)

Backend: `AGENT_ACP_TEST_CAPTURE=false`, `SUBMIT_PAYMENT=false`.
pivota-acp: `ACP_ENABLE_REAL_CAPTURE=false`.
Re-probe 3(a) → expect `lane=redirect_floor` (dark again).

## 6. Promote Wix to production (the "flag flip")

Once the canary passes, PR #1314 makes production routing honor per-store
readiness. To light up the *proven* Wix store for the production lane:

```
UPDATE merchant_stores
   SET order_writeback_status = 'enabled', order_writeback_enabled_at = NOW()
 WHERE merchant_id = '<WIX_MERCHANT_ID>' AND platform = 'wix' AND status IN ('active','connected');
```

Then merge #1314. `resolve_merchant_capability(<WIX_MERCHANT_ID>)` will return
`protocols: ["acp"]` (given a live PSP), and the production ACP lane activates —
still fail-closed behind the kill-switch (`SUBMIT_PAYMENT` + allowlist) for any
real charge. Every other Wix store stays dark until individually promoted.
