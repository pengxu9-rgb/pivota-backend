# Codex prompt — T4: Draft routes/billing_routes.py (Stripe webhook handlers + checkout session)

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Stack: Python (FastAPI), Postgres (Railway), Stripe (already integrated as PSP).
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` — implement v1.3 exactly, do not improvise on architecture.
Existing patterns to follow: see db/, services/, routes/, adapters/ — match style of existing files.
Output: code in the existing repo layout. Migrations as SQL files in db/migrations/.
Don't add new dependencies unless absolutely required. Don't rewrite existing code unless explicitly asked.

## Prerequisite inputs — read these first

1. `docs/monetization/T1_stripe_codebase_audit.md` — Stripe integration shape, existing patterns, webhook endpoint conventions, and gotchas. Read thoroughly before writing any code.
2. `db/migrations/100_stripe_events.sql` — schema of the `stripe_events` idempotency table your handlers write to.
3. `db/migrations/101_subscription_plans.sql`, `102_user_subscriptions.sql`, `103_extend_merchants_monetization.sql`, `104_merchant_credits.sql`, `106_credit_ledger.sql` — tables your handlers update on subscription events.
4. Existing `routes/webhook_routes.py` — the existing Stripe commerce webhook handler. Do NOT modify it. Read it to understand the FastAPI router pattern, signature verification helper `_stripe_webhook_secret_candidates`, and `finalize_payment_success` delegation style, so you can match conventions.

## Architecture decisions already made — implement exactly as specified

**Q1 — Stripe credentials:** Use `settings.stripe_secret_key` (the global platform key, `STRIPE_SECRET_KEY` env var) for all Billing/Invoicing/Connect SDK calls. Do NOT use merchant-scoped PSP credentials from `merchant_psps`. The platform Stripe account owns all Subscriptions, Customers, and Invoices.

**Q3 — Subscription fulfillment event:** Fulfill on `checkout.session.completed` ONLY. `customer.subscription.created` must be received, written to `stripe_events`, and then returned 200 OK without any fulfillment action (logged-only; fulfillment at this event risks duplicate writes if both fire close together).

`checkout.session.completed` fulfillment actions (in order, inside the idempotency wrapper):
1. INSERT `user_subscriptions` row keyed on `stripe_subscription_id` from the session object.
2. UPDATE `merchants` row: set `subscription_id`, `current_tier` (from plan), `credits_balance = plan.monthly_credit_allowance`, `promo_period_until = NOW() + INTERVAL '6 months'`, `billing_anchor_day = 1`.
3. INSERT `credit_ledger` row: `operation_type='subscription_initial_grant'`, `credits_delta = plan.monthly_credit_allowance`, `balance_after = plan.monthly_credit_allowance`.

`customer.subscription.updated` handles ongoing state sync (renewals, plan changes, cancellations). `customer.subscription.deleted` handles cancellation finalization — downgrade `current_tier` to `'free'`, do NOT erase ledger history.

**Q4 — Endpoint:** Create a NEW dedicated endpoint at `POST /webhooks/stripe/billing` in new file `routes/billing_routes.py`. The existing `routes/webhook_routes.py` (commerce webhooks) is UNTOUCHED. Both endpoints write idempotency rows to the same shared `stripe_events` table.

## Task

Create `routes/billing_routes.py` with:

### POST /webhooks/stripe/billing

Stripe billing webhook receiver. All billing events route here.

1. **Signature verification:** Read raw body bytes; verify using `stripe.Webhook.construct_event(payload, stripe_signature_header, settings.stripe_billing_webhook_secret)`. If `settings.stripe_billing_webhook_secret` is None or empty, raise `HTTPException(400, "Billing webhook secret not configured")` — never accept unsigned payloads on this endpoint.

2. **Idempotency check:** `INSERT INTO stripe_events (event_id, event_type, payload_jsonb, received_at, status) VALUES (...) ON CONFLICT (event_id) DO NOTHING RETURNING id`. If no row returned (duplicate), return `JSONResponse({"status": "duplicate"}, status_code=200)` immediately.

3. **Route by event_type** to internal handlers:
   - `checkout.session.completed` → `_handle_checkout_session_completed(event, db)`
   - `customer.subscription.updated` → `_handle_subscription_updated(event, db)`
   - `customer.subscription.deleted` → `_handle_subscription_deleted(event, db)`
   - `customer.subscription.created` → log-only (update `stripe_events.status='ignored'`; return 200 OK; NO fulfillment)
   - `invoice.paid` → `_handle_invoice_paid(event, db)`
   - `invoice.payment_failed` → `_handle_invoice_payment_failed(event, db)`
   - Any other event type → update `stripe_events.status='ignored'`; return 200 OK

4. **Handler contract:**
   - Each handler updates local mirror state (user_subscriptions, invoices, merchants) idempotently.
   - Each handler catches all exceptions; on exception: update `stripe_events.status='failed', error=str(e)`; re-raise as `HTTPException(500)` so Stripe retries.
   - On success: update `stripe_events.status='processed', processed_at=NOW()`.

5. **Handler internals:**

   `_handle_checkout_session_completed(event, db)`:
   - Extract `session = event.data.object`; `stripe_subscription_id = session.subscription`; `stripe_customer_id = session.customer`.
   - Lookup `subscription_plans` by `stripe_price_id = session.metadata.get('price_id')` or by matching `stripe_product_id`.
   - Execute the 3-step fulfillment sequence from Q3 above (user_subscriptions INSERT, merchants UPDATE, credit_ledger INSERT) inside a single DB transaction. Use `INSERT ... ON CONFLICT (stripe_subscription_id) DO NOTHING` on `user_subscriptions` to make it idempotent.

   `_handle_subscription_updated(event, db)`:
   - Extract `subscription = event.data.object`; status, plan, period dates.
   - UPDATE `user_subscriptions` row by `stripe_subscription_id`.
   - If plan changed: update `merchants.current_tier`.
   - If status is `canceled` or `unpaid`: treat same as deleted (downgrade tier to `'free'`).

   `_handle_subscription_deleted(event, db)`:
   - UPDATE `user_subscriptions.status = 'canceled'`.
   - UPDATE `merchants.current_tier = 'free'`, `subscription_id = NULL`.

   `_handle_invoice_paid(event, db)`:
   - Extract `invoice = event.data.object`.
   - UPDATE `invoices` row by `stripe_invoice_id`: set `status='paid'`, `paid_at=NOW()`.
   - If no matching `invoices` row, INSERT a minimal one (merchant may not have gone through billing cycle yet).

   `_handle_invoice_payment_failed(event, db)`:
   - UPDATE `invoices` row by `stripe_invoice_id`: set `status='payment_failed'`.
   - Log warning with merchant_id and invoice amount for ops alerting.

### POST /api/billing/checkout-session

Creates a Stripe Checkout Session for a merchant to subscribe to a plan.

- Auth: require valid merchant API key (reuse existing auth dependency from other routes).
- Request body: `{ "price_id": str, "success_url": str, "cancel_url": str }`.
- Look up `subscription_plans` by `stripe_price_id = price_id`.
- Look up or create Stripe Customer for the merchant: check `merchants.stripe_customer_id`; if None, call `stripe.Customer.create(email=merchant.contact_email, metadata={"merchant_id": merchant_id})` and UPDATE `merchants.stripe_customer_id`.
- Call `stripe.checkout.sessions.create(mode="subscription", customer=stripe_customer_id, line_items=[{"price": price_id, "quantity": 1}], success_url=success_url, cancel_url=cancel_url, metadata={"merchant_id": merchant_id, "price_id": price_id})`.
- Return `{"session_url": session.url, "session_id": session.id}`.

## Style requirements

- Match FastAPI router/dependency-injection patterns from `routes/webhook_routes.py` and other routes/ files.
- All Stripe SDK calls use `stripe.StripeClient(api_key=settings.stripe_secret_key)` — NOT global `stripe.api_key`. Instantiate one client at module level.
- Async route handlers; wrap sync Stripe SDK calls with `asyncio.to_thread(...)` matching `adapters/psp_adapter.py` pattern.
- Type hints + docstrings on every public function.
- Import `database` the same way other routes do.

## Acceptance criteria

- `routes/billing_routes.py` created with `POST /webhooks/stripe/billing` and `POST /api/billing/checkout-session`.
- Idempotency check uses `INSERT ... ON CONFLICT (event_id) DO NOTHING`; duplicate event_ids return 200 OK without reprocessing.
- All 6 billing event types handled (checkout.session.completed, customer.subscription.created [log-only], customer.subscription.updated, customer.subscription.deleted, invoice.paid, invoice.payment_failed). Unknown event types return 200 OK.
- `checkout.session.completed` handler executes exactly the 3-step fulfillment sequence in the specified order, inside a transaction.
- Signature verification uses `settings.stripe_billing_webhook_secret`; never accepts unsigned payloads.
- Stripe SDK initialized with platform `settings.stripe_secret_key`, not merchant PSP credentials.
- Failed handlers update `stripe_events.status='failed'` and return 500 for Stripe retry.

## Don't do

- Do NOT modify `routes/webhook_routes.py`.
- Do NOT use merchant PSP credentials for any Billing/Invoicing SDK call.
- Do NOT implement subscription business logic (credit metering, topup) in webhook handlers — that lives in `services/metering_service.py` (T5). The webhook handler only updates mirror state.
- Do NOT add a separate webhook endpoint per event type — single `/webhooks/stripe/billing` endpoint, route internally.
- Do NOT use the legacy global `stripe.api_key` pattern from `adapters/stripe_adapter.py`.
