# Proposed Fix: submit_payment must reuse the merchant PSP surface

## Scope and assumptions

This is a review-only proposal. No production, Railway, payment, git commit, or push action was taken.

The local backend clone is on `main` at `370a27561ab2eb969c9aa9ab238ea72b735db142`; the prompt says prod is `7868947f`, but that commit object is not present locally. The prompt's verified facts match the relevant workspace code, so this proposal uses local file/line evidence plus the prompt's prod observation.

## Runtime chain observed

1. Gateway entrypoint:
   - `PIVOTA-Agent/src/server.js:45101-45134` registers `POST /agent/shop/v1/invoke`.
   - The handler calls `handleInvokeRequest(...)` at `src/server.js:45121-45129`.

2. Gateway operation dispatch:
   - `handleInvokeRequest` starts at `src/server.js:33925`.
   - It resolves `ROUTE_MAP[operation]` at `src/server.js:38980-38987`.
   - Current `ROUTE_MAP.submit_payment` is `POST /agent/v2/payments/checkout-sessions` at `src/server.js:23210-23213`.
   - The base upstream URL is built from `PIVOTA_API_BASE + route.path` at `src/server.js:41118-41125`.

3. Gateway request body:
   - `buildCheckoutSessionV2Body` builds a v2 checkout-session body at `src/server.js:1819-1888`.
   - In the `submit_payment` switch branch, the gateway calls that builder and requires `quote_id` plus `expected_amount` at `src/server.js:41856-41873`.
   - The gateway sends the upstream request at `src/server.js:42429-42442`.

4. Backend v2 endpoint:
   - `routes/agent_v2.py:1044-1049` handles `POST /agent/v2/payments/checkout-sessions`.
   - It loads and authorizes the order, then constructs `CreateCheckoutIntentRequest` from order items at `routes/agent_v2.py:1052-1101`.
   - It calls `create_checkout_intent_route(...)` at `routes/agent_v2.py:1102-1106`.

5. Hosted surface mint:
   - `routes/agent_checkout_intents.py:565-570` defines `create_checkout_intent`.
   - It creates `intent_id = f"ci_{uuid.uuid4().hex}"` at `routes/agent_checkout_intents.py:669`.
   - It mints a checkout token at `routes/agent_checkout_intents.py:715`.
   - It returns `checkout_session_id`, `checkout_token`, and a Pivota checkout URL at `routes/agent_checkout_intents.py:787-798`.
   - `routes/agent_v2.py:1112-1122` wraps that response and hard-codes `"provider": "pivota_hosted_checkout"`.

6. Gateway rejection:
   - The gateway normalizer reads `checkout_session.provider` as PSP at `src/server.js:44017-44023`.
   - It rejects `pivota_hosted_checkout` with `UNSUPPORTED_PAYMENT_SURFACE` at `src/server.js:44024-44035`.
   - It returns the 502 at `src/server.js:44140-44143`.

## Root cause

`create_order` is on the merchant PSP path, but `submit_payment` is not.

The v2 order path delegates to the v1 order creator:

- `routes/agent_v2.py:904-909` handles `POST /agent/v2/orders`.
- `routes/agent_v2.py:969-990` passes `preferred_psp`, metadata, and quote data into `CreateOrderRequest`.
- `routes/agent_v2.py:992-998` calls `agent_v1_create_order`.
- `routes/agent_api.py:8451-8459` calls `routes.order_routes.create_new_order`.
- `routes/order_routes.py:4024-4030` evaluates the PR #738 scoped test PSP bypass and verifies explicit `preferred_psp`.
- `routes/order_routes.py:4264-4303` calls `create_payment_with_failover(..., enforce_live_readiness=enforce_live_readiness)`.
- `routes/order_routes.py:4346-4353` stores the PaymentIntent id, client secret, status, and PSP.
- `routes/agent_api.py:8776-8781` returns the direct PSP payment summary.
- `routes/agent_v2.py:579-588` keeps that summary in the v2 order response.

However, the current gateway `submit_payment` route ignores that already-created direct PSP surface. It calls `/agent/v2/payments/checkout-sessions`, whose implementation always creates a Pivota-hosted checkout intent and hard-codes `provider="pivota_hosted_checkout"`. That is why the probe got a `ci_...` checkout session id and no new PaymentIntent.

## Chosen minimal fix

Route standard/direct `submit_payment` calls to `/agent/v1/payments` so the backend reuses the PaymentIntent created during `create_order`.

This is the smallest correct fix because:

- `/agent/v1/payments` is already registered (`routes/agent_payment_sdk.py:39`, `main.py:938`).
- Its request model is `PaymentRequest` with `order_id` and `payment_method` (`routes/agent_payment_sdk.py:97-103`).
- It checks for an existing order payment surface before creating anything new (`routes/agent_payment_sdk.py:605-627`).
- Existing awaiting Stripe surfaces are reusable when `return_url` is absent (`tests/test_agent_payment_sdk_existing_surface.py:50-76`).
- When no reusable surface exists, v1 creation still uses `enforce_live_readiness=True` (`routes/agent_payment_sdk.py:768-786`), so the #738 test bypass must happen during `create_order`, not during `submit_payment`.

The proposed gateway patch below sends direct/card submit calls to v1 and deliberately does not forward `return_url` on that direct-reuse branch. Forwarding `return_url` makes v1 require a redirect-ready existing Stripe surface and skip reuse for `awaiting_payment` (`routes/agent_payment_sdk.py:291-318`, tested at `tests/test_agent_payment_sdk_existing_surface.py:79-104`).

Explicit delegated handler calls with `payment_handler_id` or `payment_handler_type` are left on the existing v2 checkout-session path for now. If the controlled probe includes handler fields despite wanting a Stripe/Adyen direct PSP surface, omit those handler fields or broaden `shouldSubmitPaymentUseExistingOrderMerchantPspSurface` after Claude reviews the delegated-payment implications.

## Proposed diff

```diff
diff --git a/src/server.js b/src/server.js
index <current>.. <proposed>
--- a/src/server.js
+++ b/src/server.js
@@
 function buildCheckoutSessionV2Body({
   payload = {},
   payment = {},
   metadata = {},
   clientChannel = 'shop',
@@
     }),
   });
 }
+
+function buildSubmitPaymentV1Body({
+  payload = {},
+  payment = {},
+  checkoutSessionBody = {},
+} = {}) {
+  const explicitPaymentMethod =
+    payment?.payment_method && typeof payment.payment_method === 'object' && !Array.isArray(payment.payment_method)
+      ? payment.payment_method
+      : payment?.paymentMethod && typeof payment.paymentMethod === 'object' && !Array.isArray(payment.paymentMethod)
+        ? payment.paymentMethod
+        : {};
+  const paymentMethodType = firstNonEmptyString(
+    explicitPaymentMethod.type,
+    explicitPaymentMethod.payment_method_type,
+    explicitPaymentMethod.paymentMethodType,
+    payment?.payment_method_hint,
+    payment?.paymentMethodHint,
+    payload?.payment_method_hint,
+    payload?.paymentMethodHint,
+    'card',
+  );
+
+  return pruneEmptyFields({
+    order_id: firstNonEmptyString(
+      checkoutSessionBody?.order_id,
+      payment?.order_id,
+      payment?.orderId,
+      payload?.order_id,
+      payload?.orderId,
+    ),
+    payment_method: pruneEmptyFields({
+      ...explicitPaymentMethod,
+      type: paymentMethodType,
+    }),
+    idempotency_key: firstNonEmptyString(
+      payment?.idempotency_key,
+      payment?.idempotencyKey,
+      payload?.idempotency_key,
+      payload?.idempotencyKey,
+    ),
+    save_payment_method:
+      payment?.save_payment_method === true || payment?.savePaymentMethod === true ? true : undefined,
+  });
+}
+
+function shouldSubmitPaymentUseExistingOrderMerchantPspSurface(checkoutSessionBody = {}) {
+  return (
+    !firstNonEmptyString(checkoutSessionBody?.payment_handler_id) &&
+    !firstNonEmptyString(checkoutSessionBody?.payment_handler_type)
+  );
+}
 
 function buildSearchProductsV2Body({
   payload = {},
   search = {},
@@
       case 'submit_payment': {
         const payment = payload.payment || {};
-        requestBody = buildCheckoutSessionV2Body({
+        const checkoutSessionBody = buildCheckoutSessionV2Body({
           payload,
           payment,
           metadata,
           clientChannel,
           gatewayRequestId,
         });
-        if (!requestBody.quote_id || requestBody.expected_amount == null) {
+        if (!checkoutSessionBody.quote_id || checkoutSessionBody.expected_amount == null) {
           return res.status(400).json({
             status: 'failure',
             code: 'expected_amount_required',
             reason: 'expected_amount_required',
             message: 'submit_payment requires quote_id and expected_amount from a locked quote',
           });
         }
+        if (shouldSubmitPaymentUseExistingOrderMerchantPspSurface(checkoutSessionBody)) {
+          url = `${PIVOTA_API_BASE}/agent/v1/payments`;
+          upstreamMethod = 'POST';
+          requestBody = buildSubmitPaymentV1Body({
+            payload,
+            payment,
+            checkoutSessionBody,
+          });
+          if (!requestBody.order_id) {
+            return res.status(400).json({
+              status: 'failure',
+              code: 'order_id_required',
+              reason: 'order_id_required',
+              message: 'submit_payment requires order_id from create_order',
+            });
+          }
+        } else {
+          requestBody = checkoutSessionBody;
+        }
         break;
       }
```

```diff
diff --git a/tests/integration/submit_payment_contract.test.js b/tests/integration/submit_payment_contract.test.js
index <current>.. <proposed>
--- a/tests/integration/submit_payment_contract.test.js
+++ b/tests/integration/submit_payment_contract.test.js
@@
-  it('requires quote-bound expected_amount before creating a checkout session', async () => {
+  it('requires quote-bound expected_amount before submitting payment', async () => {
@@
   it('marks processing status as backend-owned even when client_secret is present', async () => {
     nock(API_BASE)
-      .post('/agent/v2/payments/checkout-sessions', (body) => {
-        return body?.quote_id === 'quote_001' && body?.expected_amount === 2900;
+      .post('/agent/v1/payments', (body) => {
+        return (
+          body?.order_id === 'ord_001' &&
+          body?.payment_method?.type === 'card' &&
+          body?.quote_id === undefined &&
+          body?.expected_amount === undefined
+        );
       })
@@
   it('propagates explicit submit ownership fields from the backend contract', async () => {
     nock(API_BASE)
-      .post('/agent/v2/payments/checkout-sessions')
+      .post('/agent/v1/payments')
@@
   it('forwards Shop Pay handler selection and preserves delegated redirect contract', async () => {
     nock(API_BASE)
       .post('/agent/v2/payments/checkout-sessions', (body) => {
@@
   it('fails closed when upstream sends only a partial explicit contract', async () => {
     nock(API_BASE)
-      .post('/agent/v2/payments/checkout-sessions')
+      .post('/agent/v1/payments')
@@
   it('marks requires_action status as client-owned confirmation', async () => {
     nock(API_BASE)
-      .post('/agent/v2/payments/checkout-sessions')
+      .post('/agent/v1/payments')
@@
   it('maps unknown statuses to payment_status=unknown and preserves raw status', async () => {
     nock(API_BASE)
-      .post('/agent/v2/payments/checkout-sessions')
+      .post('/agent/v1/payments')
@@
   it('normalizes failed statuses to payment_failed terminal state', async () => {
     nock(API_BASE)
-      .post('/agent/v2/payments/checkout-sessions')
+      .post('/agent/v1/payments')
@@
   it('ignores explicit client ownership on terminal payment failure', async () => {
     nock(API_BASE)
-      .post('/agent/v2/payments/checkout-sessions')
+      .post('/agent/v1/payments')
@@
   it('rejects unsupported pivota hosted checkout responses', async () => {
     nock(API_BASE)
-      .post('/agent/v2/payments/checkout-sessions')
+      .post('/agent/v1/payments')
```

```diff
diff --git a/tests/integration/checkout_rollout_canary.test.js b/tests/integration/checkout_rollout_canary.test.js
index <current>.. <proposed>
--- a/tests/integration/checkout_rollout_canary.test.js
+++ b/tests/integration/checkout_rollout_canary.test.js
@@
-      .post('/agent/v2/payments/checkout-sessions', (body) => {
+      .post('/agent/v1/payments', (body) => {
         return (
           body &&
           body.order_id === 'ORD_ROLLOUT_123' &&
-          body.quote_id === 'q_rollout_123' &&
-          body.expected_amount === 2900 &&
-          body.payment_method_hint === 'card'
+          body.payment_method?.type === 'card' &&
+          body.quote_id === undefined &&
+          body.expected_amount === undefined
         );
       })
@@
-      .post('/agent/v2/payments/checkout-sessions', (body) => {
+      .post('/agent/v1/payments', (body) => {
         return (
           body &&
           body.order_id === 'ORD_ROLLOUT_RETRY' &&
-          body.quote_id === 'q_rollout_retry' &&
-          body.expected_amount === 2900 &&
-          body.payment_method_hint === 'card'
+          body.payment_method?.type === 'card' &&
+          body.quote_id === undefined &&
+          body.expected_amount === undefined
         );
       })
@@
-      .post('/agent/v2/payments/checkout-sessions', (body) => {
+      .post('/agent/v1/payments', (body) => {
         return (
           body &&
           body.order_id === 'ORD_ROLLOUT_RETRY' &&
-          body.quote_id === 'q_rollout_retry' &&
-          body.expected_amount === 2900 &&
-          body.payment_method_hint === 'card'
+          body.payment_method?.type === 'card' &&
+          body.quote_id === undefined &&
+          body.expected_amount === undefined
         );
       })
@@
-      .post('/agent/v2/payments/checkout-sessions', (body) => {
+      .post('/agent/v1/payments', (body) => {
         return (
           body &&
           body.order_id === 'ORD_ROLLOUT_GOVERNANCE' &&
-          body.quote_id === 'q_rollout_governance' &&
-          body.expected_amount === 2900
+          body.payment_method?.type === 'card'
         );
       })
```

## Interaction with PR #738 test PSP bypass

The bypass stays in the backend order-create path only:

- `routes/order_routes.py:513-553` gates test PSP bypass by `ALLOW_TEST_PSP_PROBE=1`, `TEST_PSP_PROBE_MERCHANTS`, and order metadata such as `allow_test_psp_surfaces=true`.
- `routes/order_routes.py:4242-4303` passes the resulting `enforce_live_readiness` value to `create_payment_with_failover`.
- `/agent/v1/payments` still uses `enforce_live_readiness=True` when it creates a new surface (`routes/agent_payment_sdk.py:768-786`).

So the intended controlled flow is:

1. `create_order` for `merch_efbc46b4619cfbdf` has `metadata.allow_test_psp_surfaces=true`, `preferred_psp=stripe` or `adyen`, and the server-side allowlist env is enabled.
2. `create_order` creates and stores the TEST-mode direct PSP PaymentIntent under the #738 bypass.
3. `submit_payment` calls `/agent/v1/payments` and reuses that existing order surface.
4. No #738 bypass is added to `/agent/v1/payments`, and no hosted checkout is accepted.

## Blast radius

This proposed diff affects gateway `submit_payment` calls that do not provide explicit delegated payment handler fields. That includes the controlled card/direct PSP probe and standard card direct PSP flows.

It should not affect:

- Product/search/read-only operations.
- `preview_quote`.
- `create_order`.
- Backend source code.
- Hosted Pivota checkout rejection, which remains in place.
- Explicit handler flows that include `payment_handler_id` or `payment_handler_type`; those remain on v2 pending a separate delegated payment review.

Risk to live buyers is limited by current behavior:

- The currently observed v2 path returns `pivota_hosted_checkout`, which the gateway already rejects with 502.
- The v1 path reuses an existing direct PSP surface first.
- If no existing surface is reusable, v1 can create a new PSP surface only under live readiness. For the controlled TEST merchant, the direct TEST PaymentIntent must already have been created by `create_order`.

## Alternatives rejected

1. Wire `create_checkout_intent` / v2 checkout sessions to emit direct PSP surfaces.
   - Rejected as larger and less safe. `CreateCheckoutSessionBody` has no `preferred_psp`; `create_checkout_intent` mints checkout tokens and URLs, not PSP PaymentIntents; `agent_v2.py` hard-codes `provider="pivota_hosted_checkout"`. This would require backend API, data model, and tests changes.

2. Use readiness `create_payment_intent_for_checkout`.
   - Rejected for this flow. It is behind `FEATURE_READINESS_PAYMENT_INTENT_ALPHA` (`readiness/flags.py:38-39`, `routes/readiness_internal.py:140-142`), is an internal endpoint (`routes/readiness_internal.py:509-525`), expects a readiness checkout session journal entry with `merchant_alpha_mode="real_merchant_alpha"` (`readiness/service.py:1617-1630`), and still creates new payments with `enforce_live_readiness=True` (`readiness/service.py:1686-1694`). It does not consume the agent v2 `checkout_intents` row directly and does not honor #738's order metadata bypass.

3. Merchant capability/config change.
   - Rejected. The merchant/config already allowed `create_order` to clear live readiness under the scoped #738 bypass. The hosted surface came from gateway/backend routing code, not merchant PSP selection. The v2 checkout-session path never consults `preferred_psp`.

4. Gateway-only guard relaxation for `pivota_hosted_checkout`.
   - Rejected. It would make the gateway accept the disabled hosted surface rather than returning the merchant PSP surface required by the contract.

## Tests to run after applying the diff

Gateway:

```bash
npx jest --watchman=false --runInBand tests/integration/submit_payment_contract.test.js tests/integration/checkout_rollout_canary.test.js
```

Backend:

```bash
pytest -q tests/test_agent_payment_sdk_existing_surface.py tests/test_order_routes_psp_resolution.py tests/test_quote_first_replay_idempotency.py
```

Controlled probe expectations, with no production mutation by this document:

- Phase 2 `create_order` response includes direct PSP fields: `payment.psp in {"stripe","adyen"}`, `payment.payment_intent_id` or Adyen equivalent, `client_secret`, and no `pivota_hosted_checkout`.
- Phase 3 gateway logs show upstream `POST /agent/v1/payments` for standard card/direct PSP `submit_payment`.
- Phase 3 response is not `UNSUPPORTED_PAYMENT_SURFACE`.
- The returned PSP is `stripe` or `adyen`; no `checkout_session.provider="pivota_hosted_checkout"`.

## Risks and open questions for Claude

1. Confirm the controlled probe `submit_payment` payload does not include `payment_handler_id` or `payment_handler_type`. If it does, decide whether those fields should be omitted for direct card PSP probes or whether the direct-route predicate should explicitly recognize Stripe/Adyen handlers.

2. Confirm whether the gateway should continue requiring `quote_id` and `expected_amount` for `submit_payment` even though the v1 backend route does not consume those fields. The proposed diff keeps the requirement as a quote-lock guard and forwards only v1-compatible fields.

3. Confirm whether omitting `return_url` on the v1 direct-reuse branch is acceptable for the UI. It is necessary for reusing an `awaiting_payment` Stripe PaymentIntent with the current backend code.

4. Decide whether explicit delegated payment handler flows should stay on v2, move to a separate merchant PSP endpoint, or remain blocked until there is a direct merchant-handler surface contract.

5. The local backend clone does not contain prod commit `7868947f`; verify the same line-level behavior on the deploy artifact before applying the gateway patch to prod.
