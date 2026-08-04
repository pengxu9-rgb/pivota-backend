# Adyen ACP off-session capture canary runbook (P-T2.3.7)

Proves the **Adyen** in-chat ACP money path end-to-end on real Adyen **test** mode,
the Adyen analog of the Wix/Stripe canary
([wix_acp_test_capture_canary.md](./wix_acp_test_capture_canary.md)). The capture is
a merchant-initiated transaction (MIT) against a stored payment method, driven by
`_AdyenCaptureAdapter` (pivota-backend #1330 + #1331).

> **Who runs it:** the operator. The auto-mode classifier blocks the agent from
> running the charge; Adyen Customer Area verification is yours.

## How Adyen differs from the Stripe/Wix canary (read first)

1. **Dispatch is by PSP, not platform.** The ACP lane still uses a platform
   connector (Shopify/Wix on pivota-acp), but the *capture* is dispatched by the
   merchant's **active runtime PSP**. `capture_offsession` resolves the merchant's
   active PSP with **no provider hint** and dispatches by its `provider` — so the
   canary merchant's active PSP must resolve to **Adyen** (see Prereqs).
2. **No universal test PM.** Unlike Stripe's `pm_card_visa`, Adyen has no default
   test PM. You must **tokenize an Adyen test card first** (Step 0) to obtain a
   `shopperReference` + `storedPaymentMethodId`, and pass the stored-PM id as the
   payment token in 3(b). The token is **required** — there is no fallback (the
   adapter refuses `pm_`/`tok_`/`vt_` tokens with `adyen_pm_required`).
3. **shopperReference.** The MIT needs the `shopperReference` the stored PM is
   bound to. For a **single-shopper canary**, store it once in the Adyen PSP's
   `provider_config.shopper_reference` — the adapter reads it there (fallback after
   `metadata.adyen_shopper_reference`). Production multi-buyer flows thread a
   per-order `adyen_shopper_reference` instead (a follow-up; see §7).

## Prerequisites

- A canary merchant `<ADYEN_MERCHANT_ID>` **connected to a supported platform**
  (Shopify or Wix — so the ACP lane + pivota-acp connector work).
- Its **active runtime PSP resolves to Adyen (test)** — a `merchant_psps` row:
  `provider='adyen'`, `status='active'`, `environment='test'`,
  `api_key='test_...'`, `provider_config={merchant_account, client_key,
  shopper_reference}`. **If the merchant has other active PSPs (e.g. Stripe),
  ensure Adyen is the one resolved** (`fetch_active_runtime_merchant_psp` returns
  the first active PSP by `is_primary`/`connected_at` order — deactivate the
  others or use a dedicated Adyen merchant). Confirm:
  ```sql
  SELECT psp_id, provider, status, environment, connected_at
  FROM merchant_psps WHERE merchant_id='<ADYEN_MERCHANT_ID>' AND status='active'
  ORDER BY connected_at DESC;
  ```
- Both services on current `main` (backend ≥ #1331 has the adapter + live-prefix;
  pivota-acp ≥ #28).

## 0. Tokenize an Adyen test card (once) → shopperReference + storedPaymentMethodId

There is no default Adyen test PM, so create a stored one. Via the Adyen **test**
Checkout API (or dropin with `storePaymentMethod`), make an initial payment /
zero-auth that stores the PM:

```bash
curl -sS -X POST https://checkout-test.adyen.com/v71/payments \
  -H "X-API-Key: <ADYEN_TEST_API_KEY>" -H "Content-Type: application/json" \
  -d '{
    "merchantAccount": "<ADYEN_MERCHANT_ACCOUNT>",
    "amount": {"value": 0, "currency": "USD"},
    "reference": "tokenize_canary_1",
    "paymentMethod": {"type":"scheme","number":"4111111111111111","expiryMonth":"03","expiryYear":"2030","cvc":"737","holderName":"Test"},
    "shopperReference": "<CANARY_SHOPPER_REF>",
    "storePaymentMethod": true,
    "recurringProcessingModel": "CardOnFile",
    "shopperInteraction": "Ecommerce"
  }'
```
Record the returned **`storedPaymentMethodId`** (a.k.a. `recurring.recurringDetailReference`)
and the **`<CANARY_SHOPPER_REF>`** you chose. (Test card `4111 1111 1111 1111`,
any future expiry, CVC `737`.)

Then store the shopperReference on the merchant's Adyen PSP so the adapter finds it:
```sql
UPDATE merchant_psps
   SET provider_config = provider_config || '{"shopper_reference":"<CANARY_SHOPPER_REF>"}'::jsonb
 WHERE merchant_id='<ADYEN_MERCHANT_ID>' AND provider='adyen' AND status='active';
```

## 1. Arm the BACKEND (Railway `web` env)

```
AGENT_CHECKOUT_STRICT=on
SUBMIT_PAYMENT=true
SUBMIT_PAYMENT_MERCHANTS=<ADYEN_MERCHANT_ID>
AGENT_ACP_TEST_CAPTURE=true
AGENT_ACP_TEST_MAX_CENTS=2000
```
Leave all live flags OFF (`AGENT_ACP_ALLOW_LIVE_CAPTURE` unset/false).

`FEATURE_PLATFORM_ORDERS_ACP` is gone (ADR-021): it gated the retired external
pivota-acp integration, and setting it now does nothing. The ACP lane is gated by
the lane decision + kill-switch chain — the `SUBMIT_PAYMENT*` flags above.

## 2. ~~Arm the pivota-acp SERVICE~~ — NO LONGER APPLICABLE (ADR-021)

The external pivota-acp service is retired; both calls in step 3 now hit the
backend. There is nothing to arm here — skip to step 3.

## 3. Run the chain

**3(a) — create the in-chat ACP session** (agent-authed; platform = the merchant's
connected platform, e.g. `wix` or omit to use the primary):
```bash
curl -sS -X POST https://api.pivota.cc/agent/v1/checkout/acp \
  -H "X-API-Key: <ak_ key>" -H "Content-Type: application/json" \
  -d '{"items":[{"merchant_id":"<ADYEN_MERCHANT_ID>","product_id":"<PID>","variant_id":"<VID>","quantity":1}],
       "shipping_address":{"name":"Test Buyer","address_line1":"1 Test St","city":"San Francisco","state":"CA","postal_code":"94105","country":"US"},
       "pvt_click_id":"clk_adyen_canary_1","pvt_surface":"canary"}'
```
Expect `lane: acp_in_chat`, `acp_session_id: csn_...`. (If `redirect_floor`,
recheck Step 1 + that the merchant resolves to a supported platform.)

**3(b) — complete → MIT capture on Adyen test** (⚠️ **the payment token MUST be the
`storedPaymentMethodId` from Step 0** — omitting it fails with `adyen_pm_required`):
```bash
curl -sS -X POST "https://api.pivota.cc/agent/v1/checkout/acp/<csn_...>/complete" \
  -H "X-API-Key: <ak_ key>" \
  -H "Content-Type: application/json" \
  -d '{"payment_data":{"provider":"adyen","token":"<STORED_PAYMENT_METHOD_ID>"}}'
```
Merchant and platform come from the session created in 3(a) — no `X-Merchant-Id` /
`X-Platform` headers, and the agent key must be authorized for that merchant.

Completion is in-process since PR #1669 (ADR-021): the session goes
`quote → order(acp) → payments`; `capture_offsession` resolves the merchant's Adyen
PSP → `_AdyenCaptureAdapter` → `POST https://checkout-test.adyen.com/v71/payments`
(MIT: `shopperInteraction: ContAuth`, `recurringProcessingModel: CardOnFile`,
`storedPaymentMethodId`, `shopperReference`).

## 4. Verify

- **Backend order**: `GET /agent/v1/orders/{order_id}` → `payment_status=paid`,
  merchant `<ADYEN_MERCHANT_ID>`. The `payment_intent_id` carries the Adyen
  `pspReference`.
- **Adyen Customer Area (test)**: an **Authorised** payment for the amount, with
  `shopperReference=<CANARY_SHOPPER_REF>`.
- Adapter error decoder if it fails: `adyen_config` (no merchant_account) ·
  `adyen_pm_required` (token missing or a `pm_`/`tok_`/`vt_`) ·
  `adyen_shopper_ref_required` (no shopperReference in metadata **or**
  provider_config) · `adyen_refused:<reason>` (Adyen declined) · `requires_action`
  (SCA — rare for MIT) · `adyen_http_*` (API/auth error).

> Pricing note: the ACP-session total is placeholder; the **backend** re-quote sets
> the real captured amount (same cosmetic mismatch as the Wix/Stripe canary).

## 5. Disarm (immediately after)

Backend: `AGENT_ACP_TEST_CAPTURE=false`, `SUBMIT_PAYMENT=false`.
Re-probe 3(a) → expect `redirect_floor`.

## 6. Promote Adyen to LIVE (later)

- Store the merchant's Adyen **live** API key + `provider_config.live_url_prefix`
  (the per-merchant Customer-Area prefix → `{prefix}-checkout-live.adyenpayments.com`).
  Without the prefix the live capture fails closed (`adyen_config`).
- Arm the live lane exactly as the Stripe live canary:
  `AGENT_ACP_ALLOW_LIVE_CAPTURE=true` + `AGENT_ACP_LIVE_CAPTURE_MERCHANTS` (JSON
  array to avoid the settings crash) + `AGENT_ACP_LIVE_MAX_CENTS` + pivota-acp
  `ACP_ENABLE_REAL_CAPTURE=true`.
- Use a **real** buyer stored PM (real card tokenized under a real shopperReference)
  and a **live** merchant account. Immediate-capture accounts settle on
  `Authorised`; a manual/delayed-capture account would need a separate `/captures`
  call (not built — require immediate-capture for v1).

## 7. Known follow-ups

- **Per-order shopperReference threading.** The provider_config fallback works for
  a single canary shopper. Production multi-buyer needs `adyen_shopper_reference`
  threaded per order: `/agent/v1/checkout/acp` metadata → pivota-acp session
  metadata → `real_capture_via_backend` order_metadata → `create_payment` capture
  metadata → adapter. Small change across those hops.
- **Multi-PSP resolution.** `capture_offsession` resolves the merchant's active PSP
  with no provider hint; a merchant with several active PSPs relies on ordering.
  Routing-aware selection is a follow-up (fine for a single-PSP canary merchant).
