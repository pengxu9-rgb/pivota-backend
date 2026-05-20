## 1. **Current Stripe integration shape**

The current codebase has three Stripe integration layers:

1. The active merchant PSP path: `routes/order_routes.py`, `routes/agent_payment_sdk.py`, and `routes/payment_execution_routes.py` call `adapters/multi_psp_orchestrator.py`, which builds `adapters/psp_adapter.py::StripeAdapter` from canonical `merchant_psps` rows.
2. The Stripe webhook/refund/dispute path: `routes/webhook_routes.py`, `services/refund_service.py`, `services/dispute_records_service.py`, and `routes/merchant_risk_api.py`.
3. Older compatibility/prototype paths: `adapters/stripe_adapter.py`, `psp/connectors.py`, `psp/production_connectors.py`, `orchestrator/payment_orchestrator.py`, `routes/payment_routes.py`, `routes/agent_routes.py`, and `orchestrator/payment_executor.py`.

`adapters/psp_adapter.py::StripeAdapter` is the main runtime Stripe adapter today.

- `adapters/psp_adapter.py:104` defines `class StripeAdapter(PSPAdapter)`.
- `StripeAdapter.__init__(api_key, account_id=None, mode="payment_intent", environment=None, public_key=None)` creates `stripe.StripeClient(...)` at `adapters/psp_adapter.py:120`.
- The client is initialized with:
  - `api_key`
  - `stripe_account=account_id`
  - `max_network_retries=_STRIPE_MAX_NETWORK_RETRIES`
  - `stripe.RequestsClient(timeout=(STRIPE_CONNECT_TIMEOUT_SECONDS, STRIPE_REQUEST_TIMEOUT_SECONDS))`
- This means the current active Stripe path is merchant-runtime-credential scoped when reached through `merchant_psps`; it is not automatically the platform/global Stripe account.

PaymentIntent creation in the active path:

- `adapters/psp_adapter.py:136` defines `StripeAdapter.create_payment_intent(amount: Decimal, currency: str, metadata: Dict[str, Any])`.
- For normal Stripe Payment Element flow, it calls `self._client.v1.payment_intents.create` at `adapters/psp_adapter.py:213`.
- Arguments passed today:
  - `amount=int(amount * 100)`
  - `currency=currency.lower()`
  - `metadata=metadata`
  - `automatic_payment_methods={"enabled": True, "allow_redirects": redirect_policy}`
- `redirect_policy` comes from `StripeAdapter._resolve_redirect_policy()` at `adapters/psp_adapter.py:129`:
  - `metadata["stripe_allow_redirects"]` or `metadata["allow_redirects"]` of `"always"` or `"never"` wins.
  - Any `metadata["return_url"]` makes it `"always"`.
  - Otherwise it defaults to `"never"`.
- The adapter returns the local wrapper `PaymentIntent` from `adapters/psp_adapter.py:33` with:
  - `id=payment_intent.id`
  - `client_secret=payment_intent.client_secret`
  - `amount=payment_intent.amount`
  - `currency=payment_intent.currency`
  - `status=payment_intent.status`
  - `psp_type="stripe"`
  - `raw_response` containing Stripe id, status, amount, currency, and optional `public_key`, `stripe_account`, `account_id`, `environment`.

Stripe Checkout Session creation in the active path:

- `StripeAdapter.create_payment_intent()` switches to Checkout Session when `metadata["psp_mode"] == "stripe_checkout"` or adapter `mode == "checkout_session"`.
- It calls `self._client.v1.checkout.sessions.create` at `adapters/psp_adapter.py:168`.
- Arguments passed today:
  - `mode="payment"`
  - one `line_items` entry with `quantity=1`
  - `price_data.currency=currency.lower()`
  - `price_data.unit_amount=int(amount * 100)`
  - `price_data.product_data.name=metadata["description"] or metadata["order_id"] or "Order"`
  - hard-coded `success_url="https://merchant.pivota.cc/payment/success?session_id={CHECKOUT_SESSION_ID}"`
  - hard-coded `cancel_url="https://merchant.pivota.cc/payment/cancel"`
  - `metadata=metadata`
  - `payment_intent_data={"metadata": metadata}`
- The returned local `PaymentIntent` wrapper uses:
  - `id=session.id`
  - `client_secret=None`
  - `status="requires_action"`
  - `psp_type="stripe_checkout"`
  - `redirect_url=session.url`
  - `raw_response=session`

Stripe payment confirmation and status:

- `adapters/psp_adapter.py:256` defines `StripeAdapter.confirm_payment(payment_intent_id, payment_method_id)`.
- It calls `self._client.v1.payment_intents.confirm` at `adapters/psp_adapter.py:262` with:
  - first positional arg `payment_intent_id`
  - body `{"payment_method": payment_method_id}`
- `adapters/psp_adapter.py:274` defines `StripeAdapter.get_payment_status(payment_intent_id)`.
- It calls `self._client.v1.payment_intents.retrieve` at `adapters/psp_adapter.py:280`.
- `adapters/psp_adapter.py:290` defines `StripeAdapter.get_payment_status_details(payment_intent_id)`.
  - If the reference starts with `cs_`, it calls `self._client.v1.checkout.sessions.retrieve` at `adapters/psp_adapter.py:315` with `{"expand": ["payment_intent"]}` and derives status/amount/currency from the session and expanded PaymentIntent.
  - Otherwise it calls `self._client.v1.payment_intents.retrieve` at `adapters/psp_adapter.py:344`.

Stripe refunds:

- `adapters/psp_adapter.py:358` defines `StripeAdapter.refund_payment(payment_intent_id, amount=None, reason=None, idempotency_key=None, currency=None, full_refund=None)`.
- If `payment_intent_id` starts with `cs_`, it resolves the actual PaymentIntent by calling `self._client.v1.checkout.sessions.retrieve` at `adapters/psp_adapter.py:378` with `{"expand": ["payment_intent"]}`.
- It calls `self._client.v1.refunds.create` at `adapters/psp_adapter.py:414`.
- Arguments passed today:
  - `{"payment_intent": payment_intent_ref}`
  - optional `amount=int(amount * 100)`
  - optional Stripe enum `reason` if it is one of `duplicate`, `fraudulent`, `requested_by_customer`
  - non-enum reasons go into `metadata={"reason": reason_norm}`
  - for `cs_` references, metadata also gets `checkout_session_id`
  - if `idempotency_key` is present, request options are `{"idempotency_key": str(idempotency_key)}`
- `adapters/psp_adapter.py:424` defines `StripeAdapter.get_refund_details(refund_id)` and calls `self._client.v1.refunds.retrieve` at `adapters/psp_adapter.py:429`.

Factory and orchestration:

- `adapters/psp_adapter.py:676` defines `get_psp_adapter(psp_type, api_key, **kwargs)`.
- For `psp_type == "stripe"`, it returns `StripeAdapter(api_key, account_id=..., mode=..., environment=..., public_key=...)`.
- `adapters/multi_psp_orchestrator.py:49` defines `MultiPSPOrchestrator`.
- `MultiPSPOrchestrator.load_psp_configs()` reads active canonical PSP rows through `services/merchant_psp_config_service.py::fetch_active_merchant_psps()` and makes `PSPConfig` entries.
- `MultiPSPOrchestrator.create_payment_intent(...)` at `adapters/multi_psp_orchestrator.py:130`:
  - builds adapter kwargs with `build_runtime_adapter_kwargs()`
  - calls `get_psp_adapter(...)`
  - calls `await psp_adapter.create_payment_intent(...)`
  - logs payment attempts into `payment_attempts` best-effort
  - returns `(True, payment_intent, None, config.psp_type)` or `(False, None, error, "none")`
- `adapters/multi_psp_orchestrator.py:412` exposes `create_payment_with_failover(...)`.

Order creation flow:

- `routes/order_routes.py:1937` defines `create_new_order(...)` on `POST /orders/create`.
- It resolves the active PSP row with `_resolve_active_order_psp()` at `routes/order_routes.py:388`.
- It persists the order with `db.orders.create_order(...)`.
- It calls `create_payment_with_failover(...)` at `routes/order_routes.py:2541`.
- The metadata passed to Stripe includes:
  - `order_id`
  - `merchant_id`
  - `customer_email`
  - `route_id`
  - `agent_id`
  - optional `selected_payment_offer_id`
  - optional `payment_offer_evidence_hash`
  - optional attribution fields
  - optional `psp_mode="stripe_checkout"`
  - optional `return_url`
- On success, it stores the Stripe reference through `db.orders.update_payment_info(...)` at `routes/order_routes.py:2618` with:
  - `payment_intent_id=payment_intent.id`
  - `client_secret=payment_intent.client_secret or ""`
  - `payment_status="awaiting_payment"`
  - `psp_used=final_psp`
- It builds the frontend action through `services/merchant_payment_initiation_service.py::build_payment_action()` at `routes/order_routes.py:2596`.

Order payment confirmation flow:

- `routes/order_routes.py:2839` defines `confirm_payment(...)` on `POST /orders/payment/confirm`.
- It resolves the PSP adapter with `_resolve_order_psp_adapter(order)` at `routes/order_routes.py:412`.
- It calls `await psp_adapter.confirm_payment(payment_intent_id=order["payment_intent_id"], payment_method_id=payment_request.payment_method_id)` at `routes/order_routes.py:2872`.
- On returned status `"succeeded"`, it calls `mark_order_paid(...)`, logs `payment_succeeded`, and schedules Shopify order creation.

Merchant `/payment/execute` flow:

- `routes/payment_execution_routes.py:693` defines `execute_payment(...)` on `POST /payment/execute`.
- It resolves the merchant from `X-Merchant-API-Key`.
- `_execute_payment_for_merchant(...)` at `routes/payment_execution_routes.py:341` resolves active canonical PSP candidates and calls `services/merchant_payment_initiation_service.py::initiate_merchant_payment(...)` at `routes/payment_execution_routes.py:382`.
- `initiate_merchant_payment(...)` at `services/merchant_payment_initiation_service.py:349` either:
  - instantiates an adapter from supplied candidates and calls `adapter.create_payment_intent(...)`, or
  - calls `create_payment_with_failover(...)` at `services/merchant_payment_initiation_service.py:435`.
- The `PaymentExecuteResponse` returns `payment_action`, which for Stripe PaymentIntent is `type="stripe_client_secret"`.

Agent payment SDK flow:

- `routes/agent_payment_sdk.py:311` defines `create_payment(...)` on `POST /agent/v1/payments`.
- It selects a route, builds `preferred_psps`, then calls `create_payment_with_failover(...)` at `routes/agent_payment_sdk.py:655`.
- It stores a row in `payments`, updates the order with `update_payment_info(...)`, and returns `payment_action` from `build_payment_action(...)`.
- `_build_existing_order_payment_surface(...)` at `routes/agent_payment_sdk.py:220` can rebuild a Stripe `payment_action` for an existing order using `payment_intent_id`, `client_secret`, and public key/account data from `merchant_psps`.

Readiness/internal flow:

- `routes/readiness_internal.py:508` defines an internal endpoint for checkout-session payment intent creation.
- `readiness/service.py:1610` defines `create_payment_intent_for_checkout(...)`.
- It calls `create_payment_with_failover(...)` for real-merchant alpha checkout sessions, then stores the resulting payment reference with `update_payment_info(...)`.
- `readiness/service.py:402` calls `stripe.checkout.Session.retrieve(checkout_session_id, expand=["payment_intent"])` when syncing Stripe Checkout Session status.

Legacy global Stripe adapter:

- `adapters/stripe_adapter.py:12` sets global `stripe.api_key = settings.stripe_secret_key` at import time.
- `adapters/stripe_adapter.py:14` defines sync `create_payment_intent(amount: int, currency="usd", payment_method_types=None, metadata=None)`.
- It calls `stripe.PaymentIntent.create(...)` at `adapters/stripe_adapter.py:39`.
- Arguments passed today:
  - `amount=amount`
  - `currency=currency`
  - `payment_method_types=payment_method_types or ["card"]`
  - `metadata=metadata or {}`
  - `automatic_payment_methods={"enabled": True}`
- It returns a dict with `success`, `payment_intent`, and `client_secret`, not a `PaymentIntent` wrapper.
- `adapters/stripe_adapter.py:64` defines `verify_webhook_signature(payload, signature, endpoint_secret)`, which calls `stripe.Webhook.construct_event(...)` at `adapters/stripe_adapter.py:81` and returns only `True` or `False`.
- `adapters/stripe_adapter.py:92` defines `get_payment_intent(payment_intent_id)` and calls `stripe.PaymentIntent.retrieve(...)` at `adapters/stripe_adapter.py:103`.
- `adapters/stripe_adapter.py:109` defines `confirm_payment_intent(payment_intent_id, payment_method=None)` and calls `stripe.PaymentIntent.confirm(...)` at `adapters/stripe_adapter.py:124`.

Legacy `psp.connectors` path:

- `psp/connectors.py:35` defines `StripeConnector`.
- `StripeConnector.create_payment_intent(...)` calls `stripe.PaymentIntent.create(...)` at `psp/connectors.py:51` with:
  - `amount=int(request.amount * 100)`
  - `currency=request.currency.lower()`
  - `payment_method_types=["card"]`
  - metadata containing `order_id`, `customer_email`, and request metadata
- `StripeConnector.confirm_payment(...)` calls `stripe.PaymentIntent.retrieve(...)` at `psp/connectors.py:90`.
- `psp/connectors.py:318` defines `PSPManager`, and `psp/connectors.py:397` creates the global `psp_manager`.
- No code in the repo registers a `StripeConnector` into this global `psp_manager`.
- `orchestrator/payment_orchestrator.py:33` defines `PaymentOrchestrator`, and `routes/payment_routes.py:50` calls it from `POST /api/payments/process`; this path depends on the unregistered global `psp_manager`.

Legacy production connector path:

- `psp/production_config.py:9` defines `PRODUCTION_PSP_CONFIG["stripe"]` from environment.
- `psp/production_connectors.py:17` defines `ProductionStripeConnector`.
- `ProductionStripeConnector.create_payment_intent(...)` calls `stripe.PaymentIntent.create(...)` at `psp/production_connectors.py:36` with:
  - `amount=int(request.amount * 100)`
  - `currency=request.currency.lower()`
  - `payment_method_types=["card"]`
  - `capture_method="automatic"`
  - metadata including `order_id`, `customer_email`, `merchant_id`, `agent_id`
  - `description=f"Payment for order {request.order_id}"`
  - `receipt_email=request.customer_email`
- `ProductionStripeConnector.confirm_payment(...)` calls `stripe.PaymentIntent.retrieve(...)` at `psp/production_connectors.py:84`.
- `psp/production_connectors.py:313` creates `production_psp_manager`, but the main payment routes import `psp.connectors.psp_manager`, not this production manager.

Stripe disputes/risk:

- `services/dispute_records_service.py:55` defines `_ensure_stripe_api_key()`, which uses global `stripe.api_key` and falls back to `settings.stripe_secret_key`.
- `services/dispute_records_service.py:101` defines `_stripe_charge_lookup_best_effort(charge_id)` and calls `stripe.Charge.retrieve(...)` at `services/dispute_records_service.py:107`.
- `services/dispute_records_service.py:113` defines `_stripe_payment_intent_lookup_best_effort(payment_intent_id)` and calls `stripe.PaymentIntent.retrieve(...)` at `services/dispute_records_service.py:119`.
- `routes/merchant_risk_api.py:283` defines `sync_disputes(...)`.
  - It sets global `stripe.api_key` from `settings.stripe_secret_key`.
  - It calls `stripe.Charge.list(payment_intent=payment_intent_id, limit=10)` at `routes/merchant_risk_api.py:320`.
  - It calls `stripe.Dispute.list(charge=charge_id, limit=limit)` at `routes/merchant_risk_api.py:264`, with fallback `stripe.Dispute.list(limit=limit)` at `routes/merchant_risk_api.py:270`.

Files from the requested list:

- `adapters/stripe_adapter.py` is a legacy sync global-key wrapper.
- `routes/payment_routes.py` is not a Stripe route today. Its webhook endpoint at `routes/payment_routes.py:249` is for Checkout.com, not Stripe.
- `orchestrator/payment_orchestrator.py` uses `psp.connectors.psp_manager`, not the current canonical merchant PSP adapter path.
- `services/psp_payment_finalizer.py` is PSP-generic finalization used by Stripe, Adyen, and Checkout.com webhook handlers.
- `config/settings.py` references Stripe environment variables listed below.
- `PAYMENT_TESTING_COORDINATION.md` is not present at the repo root in this branch.

There are no current code references to `stripe.Subscription.create`, `stripe.Invoice.create`, or `stripe.InvoiceItem.create`. There is no current global `stripe.checkout.Session.create(...)` call; the active adapter uses `self._client.v1.checkout.sessions.create(...)`.

## 2. **Function signatures and conventions**

Active adapter conventions:

- `adapters/psp_adapter.py::PSPAdapter` is async and tuple-returning.
- `create_payment_intent(amount: Decimal, currency: str, metadata: Dict[str, Any]) -> Tuple[bool, Optional[PaymentIntent], Optional[str]]`
- `confirm_payment(payment_intent_id: str, payment_method_id: str) -> Tuple[bool, str, Optional[str]]`
- `get_payment_status(payment_intent_id: str) -> Tuple[bool, str, Optional[str]]`
- `refund_payment(payment_intent_id: str, amount: Optional[Decimal], reason: Optional[str], idempotency_key: Optional[str], currency: Optional[str], full_refund: Optional[bool]) -> Tuple[bool, Optional[str], Optional[str]]`
- Stripe SDK calls are sync calls wrapped with `asyncio.to_thread(...)`.
- Exceptions are caught and converted to `(False, ..., str(e))`; callers generally do not catch Stripe SDK exception types.

Local payment wrapper:

- `adapters/psp_adapter.py:33` defines `PaymentIntent`.
- It is a simple class, not a dataclass or Pydantic model.
- Fields are `id`, `client_secret`, `amount`, `currency`, `status`, `psp_type`, `raw_response`, `redirect_url`, and `public_key`.
- For Stripe Checkout Sessions, `id` can be `cs_...` and `client_secret` is `None`.
- For Stripe PaymentIntents, `id` is `pi_...` and `client_secret` is returned to frontend clients.

Orchestrator conventions:

- `adapters/multi_psp_orchestrator.py::create_payment_with_failover(...)` returns `(success, payment_intent, error, psp_used)`.
- The canonical PSP source is `merchant_psps`, fetched through `services/merchant_psp_config_service.py`.
- Preferred PSPs are a list of provider strings like `["stripe", "adyen", "checkout"]`.
- `canonical_psp_required` is accepted but `load_psp_configs(canonical_only=...)` now always uses canonical `merchant_psps`.
- `enforce_live_readiness=True` skips PSP configs where `evaluate_psp_readiness(...)["live_charge_ready"]` is false.

Dependency-injection patterns:

- FastAPI routes use `Depends(...)` for auth and request context.
- Stripe adapter construction is not injected through FastAPI. It is created at runtime through `get_psp_adapter(...)`.
- Merchant-specific runtime credentials come from `merchant_psps` rows:
  - `api_key`
  - `account_id`
  - `secret_key`
  - `environment`
  - `provider_config`
- `services/merchant_psp_config_service.py::build_runtime_adapter_kwargs(...)` owns normalization of Stripe adapter kwargs.

Error handling style:

- Routes raise `HTTPException`.
- Payment adapters return tuple errors instead of raising.
- Finalizers in `services/psp_payment_finalizer.py` return dicts such as `{"applied": True, ...}` or `{"applied": False, "reason": ...}`.
- Webhook handlers are designed to be replay-safe at the order-state level, but Stripe webhook events are not recorded in `webhook_events` the way the Checkout.com route does.
- Best-effort side effects usually catch and suppress exceptions after logging. Examples include merchant webhook emission, PCS evidence pack creation, Shopify order creation, dispute evidence pack creation, and attribution updates.

PSP-generic finalizer conventions:

- `services/psp_payment_finalizer.py:99` defines `finalize_payment_success(order, *, psp, payment_reference, transaction_id=None, amount_minor=None, currency=None, source_event="payment_confirmed_webhook", metadata_extra=None, update_payment_info_fn=None, mark_order_paid_fn, log_order_event_fn)`.
- `services/psp_payment_finalizer.py:170` defines `finalize_payment_failure(...)`.
- `services/psp_payment_finalizer.py:228` defines `finalize_refund_success(...)`.
- `services/psp_payment_finalizer.py:319` defines `finalize_refund_failure(...)`.
- `services/psp_payment_finalizer.py:412` defines `finalize_cancellation(...)`.
- These functions receive DB mutation functions as arguments instead of importing every mutation internally.

Frontend action conventions:

- `services/merchant_payment_initiation_service.py:200` defines `build_payment_action(payment_intent, *, psp_used)`.
- For Stripe PaymentIntent with a client secret, it returns:
  - `type="stripe_client_secret"`
  - `client_secret`
  - `public_key`
  - `stripe_account`
  - `raw`
  - surface contract fields such as `confirmation_owner="client"` and `component_kind="stripe_payment_element"`
- For any payment wrapper with `redirect_url`, it returns `type="redirect_url"`.

Legacy conventions:

- `adapters/stripe_adapter.py` functions are sync and dict-returning.
- `routes/agent_routes.py` and `orchestrator/payment_executor.py` call `await create_payment_intent(...)` from this sync module; that is inconsistent with the function signature.
- The legacy function returns `{"payment_intent": intent, "client_secret": ...}`, but those callers access `intent["id"]`.

## 3. **Env vars and config**

Stripe env vars directly referenced in current settings/config:

- `STRIPE_SECRET_KEY`
  - `config/settings.py:52` maps it to `settings.stripe_secret_key`.
  - `config/production.py:55` maps it to `ProductionSettings.stripe_secret_key`.
  - `adapters/stripe_adapter.py:12` assigns it to global `stripe.api_key` at import time.
  - `services/dispute_records_service.py:65` uses it as fallback global API key for dispute enrichment.
  - `routes/merchant_risk_api.py:237` uses it as fallback global API key for dispute sync.
  - `psp/production_config.py:12` uses it for legacy `PRODUCTION_PSP_CONFIG["stripe"]["api_key"]`.
- `STRIPE_WEBHOOK_SECRET`
  - `config/settings.py:53` maps it to `settings.stripe_webhook_secret`.
  - `config/production.py:56` maps it to `ProductionSettings.stripe_webhook_secret`.
  - `routes/webhook_routes.py:480` uses it as the global fallback Stripe webhook secret.
  - `psp/production_config.py:14` uses it for legacy `PRODUCTION_PSP_CONFIG["stripe"]["webhook_secret"]`.
- `STRIPE_PUBLISHABLE_KEY`
  - `psp/production_config.py:13` uses it for legacy `PRODUCTION_PSP_CONFIG["stripe"]["publishable_key"]`.
  - The active canonical merchant PSP path does not read this env var directly; it reads public key material from `merchant_psps.provider_config`.
- `STRIPE_REQUEST_TIMEOUT_SECONDS`
  - `adapters/psp_adapter.py:18` controls Stripe request timeout, default `"20"`.
- `STRIPE_CONNECT_TIMEOUT_SECONDS`
  - `adapters/psp_adapter.py:23` controls Stripe connect timeout, default `"5"`, capped at request timeout.
- `STRIPE_MAX_NETWORK_RETRIES`
  - `adapters/psp_adapter.py:29` controls Stripe client network retries, default `"1"`.

Stripe config stored in canonical `merchant_psps` rows:

- `services/merchant_psp_config_service.py:118` defines `normalize_provider_config(...)`.
- For Stripe, accepted `provider_config` fields are:
  - `mode`: `"payment_intent"` or `"checkout_session"`, default `"payment_intent"`
  - `public_key`, `publicKey`, `publishable_key`, or `publishableKey`
  - `webhook_endpoint_id`
  - `webhook_endpoint_secret`
  - `webhook_url`
- `account_id` is normalized into provider config and passed to `StripeAdapter` as `stripe_account`.
- `services/merchant_psp_config_service.py:343` defines `build_runtime_adapter_kwargs(...)`.
- For Stripe it returns:
  - `mode`
  - `environment`
  - optional `account_id`
  - optional `public_key`
- `services/merchant_psp_config_service.py:396` defines `evaluate_psp_readiness(...)`.
- For Stripe readiness, it checks:
  - processor status is active
  - API key exists
  - environment is live unless test surfaces are allowed upstream
  - mode is valid
  - public key is present when mode is `payment_intent`
  - live webhook endpoint is configured by `webhook_endpoint_id` plus `webhook_endpoint_secret`
  - validation status is valid

Non-Stripe but adjacent env vars in requested files:

- `CHECKOUT_WEBHOOK_SECRET` is read inside `routes/payment_routes.py:293` for the Checkout.com webhook endpoint. It is not a Stripe secret.

Undefined or indirect settings:

- `routes/agent_payout_management.py:199` references `settings.stripe_account_id`, but `config/settings.py` does not define `stripe_account_id`.
- `settings.paypal_*` are referenced by refund fallback code, but are not Stripe-related.

## 4. **Extension points for Stripe Billing (subscriptions)**

There is no current Billing/subscription implementation. No current code calls `stripe.Subscription.create`, and no route handles subscription lifecycle events.

The existing module that owns active Stripe SDK calls is `adapters/psp_adapter.py::StripeAdapter`.

- Raw Stripe Billing calls would fit beside the existing Stripe methods in `StripeAdapter` because:
  - it already owns `stripe.StripeClient` construction
  - it already supports merchant `account_id` through `stripe_account`
  - it already wraps sync Stripe SDK work in `asyncio.to_thread(...)`
  - it already returns tuple-style errors instead of raising Stripe exceptions
- For Checkout-based subscription starts, the current convention is not global `stripe.checkout.Session.create(...)`; it is `self._client.v1.checkout.sessions.create(...)`.
- The current Checkout Session method at `adapters/psp_adapter.py:168` is payment-only (`mode="payment"`). A Billing Checkout Session would be a separate StripeAdapter method, with `mode="subscription"` per v1.3 inputs, not an overload of order PaymentIntent creation unless the blueprint says so.
- If v1.3 requires direct subscription creation, the same adapter is the place where a `self._client.v1.subscriptions.create(...)` call would match current SDK style. There is no existing direct subscription method to extend.

Existing caller slots that already create PSP payment surfaces:

- `services/merchant_payment_initiation_service.py::initiate_merchant_payment(...)` owns normalized initiation responses for merchant-facing payment surfaces.
- `routes/payment_execution_routes.py::_execute_payment_for_merchant(...)` owns merchant API payment execution.
- `routes/order_routes.py::create_new_order(...)` owns commerce order payment intent creation.
- `routes/agent_payment_sdk.py::create_payment(...)` owns agent SDK payment surface creation.

For subscriptions specifically, the current order-payment routes are tied to commerce orders and `orders.payment_intent_id`. If v1.3 Billing is monetization billing rather than buyer order payment, there is no existing monetization Billing service in the codebase today. The implementation owner should be whatever v1.3 defines for monetization, with raw Stripe calls delegated to the same Stripe SDK ownership pattern above.

Config extension points already present:

- `services/merchant_psp_config_service.py::default_capabilities_for_provider("stripe")` returns `["payments", "refunds", "payouts", "subscriptions"]`.
- `merchant_psps.provider_config["mode"]` currently supports only `"payment_intent"` and `"checkout_session"`.
- No current code stores Stripe Customer IDs, Subscription IDs, Price IDs, Product IDs, Test Clock IDs, or Billing Portal sessions in a dedicated monetization model.

Webhook extension point:

- `routes/webhook_routes.py::handle_stripe_webhook(...)` is the existing Stripe webhook endpoint.
- It currently handles PaymentIntent, refund, and dispute events only.
- Subscription events such as `customer.subscription.*`, `invoice.*`, and `checkout.session.completed` are not handled today.
- Signature verification should reuse `_stripe_webhook_secret_candidates(psp_id)` at `routes/webhook_routes.py:446` if v1.3 uses the same endpoint and secret model.

## 5. **Extension points for Stripe Invoicing (GMV-take)**

There is no current Stripe Invoicing implementation. No current code calls `stripe.Invoice.create` or `stripe.InvoiceItem.create`.

The existing raw SDK owner for active Stripe calls is still `adapters/psp_adapter.py::StripeAdapter`.

- Draft invoice creation would fit as a new StripeAdapter method beside:
  - `create_payment_intent(...)`
  - `get_payment_status_details(...)`
  - `refund_payment(...)`
- The current adapter pattern would wrap calls with `asyncio.to_thread(...)` and return tuple/dict results instead of raising Stripe exceptions to callers.
- The current SDK style would be `self._client.v1.invoices.create(...)` and `self._client.v1.invoice_items.create(...)` if using the same `StripeClient` convention.
- If v1.3 explicitly requires global-style `stripe.Invoice.create(...)` and `stripe.InvoiceItem.create(invoice=...)`, the existing global-style Stripe module is `adapters/stripe_adapter.py`, but that module is legacy, sync, globally keyed, and not used by the active merchant PSP flow.

Existing paid-order hook points for GMV facts:

- `routes/webhook_routes.py::handle_stripe_webhook(...)` handles `payment_intent.succeeded` at `routes/webhook_routes.py:597`.
- It calls `_finalize_stripe_payment_success(...)` at `routes/webhook_routes.py:281`, which delegates to `services/psp_payment_finalizer.py::finalize_payment_success(...)`.
- `finalize_payment_success(...)` marks the order paid and logs an order event.
- `db.orders.mark_order_paid(...)` at `db/orders.py:555` is the shared DB transition for paid orders.
- `routes/order_routes.py::confirm_payment(...)` at `routes/order_routes.py:2839` is the client-confirmation path that also marks paid.
- `routes/payment_routes.py::checkout_webhook(...)` at `routes/payment_routes.py:249` finalizes Checkout.com payments, not Stripe, but uses the same `finalize_payment_success(...)` helper.

Existing order fields available for a GMV-take invoice calculation:

- `db/orders.py` defines `orders.total`, `orders.currency`, `orders.merchant_id`, `orders.payment_intent_id`, `orders.psp_used`, `orders.paid_at`, `orders.metadata`, and `orders.total_refunded`.
- `routes/webhook_routes.py` passes Stripe `amount` and `currency` from webhook payloads into finalization metadata.
- `services/psp_payment_finalizer.py` records `last_payment_confirmation` metadata with `psp`, `payment_reference`, `amount_minor`, `currency`, and `received_at`.

Existing refund facts that may affect GMV-take:

- `services/refund_service.py::RefundService.create_refund(...)` creates `refund_records` and calls PSP refunds.
- `routes/webhook_routes.py` handles `charge.refunded`, `refund.created`, `refund.updated`, and `refund.failed`.
- `services/psp_payment_finalizer.py::finalize_refund_success(...)` updates order refund state and tracks PSP refund refs in metadata.

The current code has no owner for draft invoice lifecycle, invoice item batching, invoice finalization, invoice sending, invoice collection status, or invoice-to-merchant mapping. Those would need to attach to v1.3 monetization ownership rather than the legacy `/api/payments` route.

## 6. **Existing webhook handling**

Stripe webhook endpoints:

- `routes/webhook_routes.py:545` registers `POST /webhooks/stripe/{psp_id}`.
- `routes/webhook_routes.py:546` registers `POST /webhooks/stripe`.
- `routes/psp_routes.py:425` registers compatibility alias `POST /psp/webhook/stripe`, which imports and calls `routes.webhook_routes.handle_stripe_webhook(...)`.

Signature verification:

- `routes/webhook_routes.py:446` defines `_stripe_webhook_secret_candidates(psp_id)`.
- If `psp_id` is present, it looks up `merchant_psps.provider_config` for provider `stripe` and reads `webhook_endpoint_secret`.
- It then appends global `settings.stripe_webhook_secret` if set.
- `routes/webhook_routes.py:571` calls `stripe.Webhook.construct_event(payload, stripe_signature, webhook_secret)` for each candidate secret.
- A `ValueError` raises `400 Invalid payload`.
- Signature failures across all candidates raise `400 Invalid signature`.
- If no candidate secret exists, the code parses `json.loads(payload)` without signature verification.

Events currently handled by `handle_stripe_webhook(...)`:

- `payment_intent.succeeded` at `routes/webhook_routes.py:597`
  - Resolves order by `orders.payment_intent_id == payment_intent_id`.
  - Falls back to `metadata.order_id`.
  - If metadata order exists but the stored reference differs, updates order `payment_intent_id` and `psp_used="stripe"`.
  - Calls `_finalize_stripe_payment_success(...)`.
  - Emits merchant webhook `payment.completed` best-effort.
  - Creates PCS order snapshot evidence best-effort.
  - Creates Shopify order best-effort unless metadata flags skip platform order creation.
- `payment_intent.payment_failed` at `routes/webhook_routes.py:661`
  - Resolves order the same way.
  - Reads `last_payment_error.message`.
  - Calls `_finalize_stripe_payment_failure(...)`.
  - Emits merchant webhook `payment.failed` best-effort.
- `charge.refunded` at `routes/webhook_routes.py:694`
  - Reads `charge.id`, `payment_intent`, `amount_refunded`, `currency`.
  - Looks up order by `orders.payment_intent_id`.
  - Calls `_finalize_stripe_refund_success(...)`.
- `refund.created` at `routes/webhook_routes.py:730`
  - Resolves order by refund `payment_intent` or refund metadata `order_id`.
  - Persists refund observability and logs `refund_created_webhook`.
  - Does not mark the order refunded.
- `refund.updated` at `routes/webhook_routes.py:766`
  - If status is `succeeded`, calls `_finalize_stripe_refund_success(...)`.
  - If status is `failed`, calls `_finalize_stripe_refund_failure(...)`.
  - Otherwise logs pending/updated refund state.
- `refund.failed` at `routes/webhook_routes.py:866`
  - Calls `_finalize_stripe_refund_failure(...)`.
- `charge.dispute.*` at `routes/webhook_routes.py:899`
  - Upserts dispute records best-effort through `services.dispute_records_service.upsert_stripe_dispute_record_best_effort(...)`.
  - Creates dispute evidence pack best-effort.
  - Does not mutate order payment state.

Events not handled today:

- `checkout.session.completed`
- `customer.subscription.*`
- `invoice.*`
- `payment_method.*`
- `customer.*`
- Test Clock events

Other webhook endpoints:

- `routes/payment_routes.py:249` defines `POST /api/payments/webhooks/checkout` for Checkout.com.
- It verifies `Cko-Signature` with `CHECKOUT_WEBHOOK_SECRET` if configured.
- It records webhook events through `WebhookService`.
- It finalizes success-like Checkout.com events through `finalize_payment_success(...)`.
- This route is not Stripe despite living in `routes/payment_routes.py`.

## 7. **Gotchas**

- `PAYMENT_TESTING_COORDINATION.md` is absent at the repo root in this branch.
- The active Stripe adapter is `adapters/psp_adapter.py::StripeAdapter`, not `adapters/stripe_adapter.py`.
- `adapters/stripe_adapter.py` is sync and globally keyed. Some deprecated/prototype callers use it as if it were async and as if it returned `{"id": ...}`.
- `routes/payment_routes.py` is not the main Stripe flow. Its `/api/payments/process` path uses the legacy in-memory `dashboard_core` plus `psp.connectors.psp_manager`; no connectors are registered into that global manager in the repo.
- `psp/production_connectors.py` creates `production_psp_manager`, but main routes do not import it.
- `adapters/ap2_payment_adapter.py` imports `StripeAdapter` from `.stripe_adapter`, but `adapters/stripe_adapter.py` does not define a `StripeAdapter` class.
- Current PaymentIntent creation in `StripeAdapter` converts `Decimal` to minor units with `int(amount * 100)`. Status/refund helpers understand zero- and three-decimal currencies, but creation does not.
- Current PaymentIntent and Checkout Session creation do not pass Stripe idempotency keys. Refund creation does support a Stripe idempotency key.
- Stripe Checkout Session creation ignores the `return_url` metadata that `routes/order_routes.py` passes and uses hard-coded success/cancel URLs.
- Stripe Checkout Session orders can store `orders.payment_intent_id` as `cs_...` initially. Later `payment_intent.succeeded` webhooks can overwrite it with `pi_...` by resolving `metadata.order_id`.
- The webhook endpoint accepts unsigned JSON if neither merchant nor global Stripe webhook secret is configured.
- Stripe webhook events are not persisted in the same explicit event table used by the Checkout.com webhook route.
- There is no `checkout.session.completed` handler, even though Stripe Checkout Session mode exists.
- There are no `invoice.*` or `customer.subscription.*` handlers.
- `services/merchant_psp_config_service.py::default_capabilities_for_provider("stripe")` includes `"subscriptions"`, but no subscription code exists.
- `routes/merchant_onboarding_routes.py::setup_psp(...)` validates Stripe keys and writes legacy onboarding/payment-router state. The active runtime payment path reads canonical `merchant_psps`; canonical writes are handled elsewhere, for example `routes/admin_api.py` through `persist_canonical_merchant_psp(...)`.
- `settings.stripe_account_id` is referenced in `routes/agent_payout_management.py`, but it is not defined in `config/settings.py`.
- The active adapter can pass `stripe_account=account_id`. A monetization Billing/Invoicing implementation must be deliberate about whether it is using merchant PSP credentials or the platform/global Stripe key, because the existing commerce PSP path is merchant scoped.
- `orders.payment_intent_id` is unique and currently overloaded to store PSP payment references, including `pi_...`, `cs_...`, and non-Stripe PSP references. It is not a general-purpose Stripe Billing or Invoicing reference field today.
