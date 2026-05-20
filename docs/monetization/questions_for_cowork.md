# Architectural Questions for Cowork — Pivota Monetization v1.3

Questions surfaced during Wave 1 audit review. Each question blocks one or more downstream tasks.
Append answers here; Jack will incorporate into blueprint v1.4+ as needed.

---

## Q1 — Platform vs. merchant-scoped Stripe key for Billing and Invoicing

**Blocks:** T4 (webhooks), T7 (invoice generation), T8 (partner settlement)

**What Codex was asked / what T1 found:**
The active commerce PSP path is *merchant-scoped*: `adapters/psp_adapter.py::StripeAdapter` is constructed from `merchant_psps` rows and passes `stripe_account=<merchant_connect_account_id>` on every SDK call. This means today's Stripe calls are made on behalf of individual merchants, not the Pivota platform.

Subscriptions (T4) and GMV-take invoices (T7) are the *platform* billing merchants — Pivota charges merchants directly. That requires the platform's own Stripe account and credentials, not the merchant's connect account.

**Decision needed:**
Which Stripe credential set should T4/T7/T8 use for subscription + invoice operations?

- **Option A (recommended):** Use the global platform key `settings.stripe_secret_key` (`STRIPE_SECRET_KEY` env var) for all Billing/Invoicing. Subscriptions, Customers, and Invoices live on the Pivota platform Stripe account. Merchant PSP credentials stay in `merchant_psps` for commerce payments only.
- **Option B:** Create a separate Stripe Connect platform account for monetization, with a new env var (e.g., `STRIPE_BILLING_SECRET_KEY`). Isolates billing from commerce PSP failures.

**Recommended resolution:** Option A unless the platform already has a separate billing Stripe account. Implementors need this confirmed before T4/T7 can be finalized.

**🟢 COWORK DECISION (May 2026): Option A confirmed.** Use `settings.stripe_secret_key` (`STRIPE_SECRET_KEY` env var) — the global platform key — for all Billing/Invoicing/Connect operations (Customers, Subscriptions, Invoices, InvoiceItems, Transfers to channel partners). Merchant-scoped PSP credentials in `merchant_psps` remain untouched — they continue to serve the brand→consumer commerce PaymentIntent flow only. The two flows have different security postures and different scaling concerns; keeping them on separate credentials makes that boundary explicit. Option B (separate billing Stripe account) is deferred to v2+ — only revisit if billing volume warrants account isolation, which it doesn't at cohort #1 scale.

---

## Q2 — `merchant_id` type in new monetization tables

**Blocks:** T3 (migrations) — affects correctness of the output Codex is currently writing

**What T2 found:**
There are two parallel merchant identity systems in the codebase:

1. `merchants.id` — `INTEGER` (SERIAL). The table being extended with `subscription_id`, `current_tier`, etc.
2. Operational `merchant_id` — `VARCHAR(50)` strings (e.g., `merch_efbc46b4619cfbdf`) used in `orders.merchant_id`, `commerce_attribution_edges.merchant_id`, `agent_payouts.merchant_id`. These align with `merchant_onboarding.merchant_id`, **not** with `merchants.id`.

New monetization tables (`user_subscriptions`, `merchant_credits`, `gmv_attribution_daily`, etc.) need to reference merchants. Which ID to use is ambiguous:

- **Option A: `merchant_id VARCHAR(50)` (operational string)** — consistent with `orders`, `commerce_attribution_edges`, `agent_payouts`. No FK to `merchants`. Allows direct join to commerce tables.
- **Option B: `merchant_id INTEGER REFERENCES merchants(id)`** — FK to the `merchants` table being extended. Correct relational integrity, but cannot join commerce tables directly.
- **Option C: Both** — store the integer `merchants.id` as the FK and denormalize the string `merchant_id` for commerce joins.

**Recommended resolution:** Option A — match the operational `VARCHAR(50)` convention used by all commerce tables. The billing system will query `gmv_attribution_daily` joined to `orders` and `commerce_attribution_edges`; a type mismatch there would require casting or subqueries on every billing run. The `merchants` table INTEGER FK can be added separately once a lookup table bridges the two ID spaces.

If Option B or C is chosen, T3 must be re-prompted before its migrations are applied.

**🟢 COWORK DECISION (May 2026): Option A confirmed (implicitly already chosen by Codex).** `merchant_id VARCHAR(50)` matching the operational string convention used by `orders`, `commerce_attribution_edges`, `agent_payouts`. This preserves the ability to JOIN billing tables directly to commerce tables without casting. The `merchants.id INTEGER` is the row's autoincrement primary key inside the merchants table only; it's not used as an FK target by commerce-domain tables. T3 migrations as written are correct; no re-prompt needed.

Future cleanup (post-Markato-ready, not in v1.3 scope): consider a lookup view `merchant_id_resolver` that maps `merchants.id INTEGER ↔ merchant_id VARCHAR(50)` for the rare cases code needs to traverse both spaces. Not needed for v1.

---

## Q3 — Subscription fulfillment event: `checkout.session.completed` vs `customer.subscription.created`

**Blocks:** T4 (billing routes / webhook handlers)

**What T1 found:**
There is no existing `checkout.session.completed` or `customer.subscription.*` handler today. T4 builds from scratch.

When a merchant completes a Stripe Checkout Session for a subscription plan, Stripe fires two events in sequence:
1. `checkout.session.completed` — fires first; contains `subscription` ID
2. `customer.subscription.created` — fires shortly after; contains full subscription object

**Decision needed:** Which event should create the local `user_subscriptions` row and mark the merchant's tier active?

- **Option A (recommended):** Fulfill on `checkout.session.completed`. Create the `user_subscriptions` row there. Use `customer.subscription.updated` for ongoing state sync (renewals, cancellations, upgrades). This is the Stripe-recommended pattern and fires reliably even if `customer.subscription.created` is delayed.
- **Option B:** Fulfill on `customer.subscription.created`. Simpler — full subscription object available immediately. Risk: rare race condition where `checkout.session.completed` fires but `customer.subscription.created` is delayed, leaving merchant in limbo.

**Recommended resolution:** Option A. T4 should handle `checkout.session.completed` as the primary fulfillment event.

**🟢 COWORK DECISION (May 2026): Option A confirmed.** Fulfillment happens on `checkout.session.completed`. T4's handler does these actions, in order, all inside the idempotent `stripe_events` wrapper:
1. Insert `user_subscriptions` row keyed on `stripe_subscription_id` from the session object.
2. Update `merchants` row with `subscription_id`, `current_tier`, `credits_balance = tier.monthly_credit_allowance`, `promo_period_until = signup_date + 6 months`, `billing_anchor_day = 1` (first-of-month per v1.3 §1.3).
3. Insert initial `credit_ledger` row (`operation_type='subscription_initial_grant'`) to record the tier allowance.

`customer.subscription.updated` handles ongoing state sync (renewals, plan changes, cancellations).
`customer.subscription.deleted` handles cancellation finalization (downgrade tier to inactive, do not erase ledger).
`customer.subscription.created` is logged (for the stripe_events audit) but NOT used for fulfillment. This avoids the duplicate-fulfillment risk if the two events arrive out of order.

---

## Q4 — Billing webhooks: same endpoint as commerce or dedicated endpoint?

**Blocks:** T4 (billing routes)

**What T1 found:**
The existing Stripe webhook endpoint at `routes/webhook_routes.py` handles commerce events: `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.refunded`, `refund.*`, `charge.dispute.*`. It uses per-merchant PSP webhook secrets for signature verification, with a global `settings.stripe_webhook_secret` fallback.

v1.3 billing adds: `invoice.paid`, `invoice.payment_failed`, `customer.subscription.*`, `checkout.session.completed`.

**Decision needed:** Should billing webhook events go to the existing `/webhooks/stripe` endpoint, or a new dedicated endpoint?

- **Option A: Dedicated endpoint** (e.g., `POST /webhooks/stripe/billing`) with its own Stripe webhook registration and a new env var `STRIPE_BILLING_WEBHOOK_SECRET`. Isolated failure domain — a billing webhook bug cannot take down commerce payment confirmations, and vice versa.
- **Option B: Shared endpoint** — add billing event handlers to the existing `handle_stripe_webhook(...)` in `routes/webhook_routes.py`. Simpler deployment, but couples billing and commerce webhook processing. The idempotency pattern (INSERT INTO `stripe_events` ... ON CONFLICT) from T3/T4 would need to coexist with the existing non-persisted commerce event handling.

**Recommended resolution:** Option A. The existing commerce webhook endpoint has no `stripe_events` persistence; bolting billing handlers onto it without that foundation creates inconsistency. A new endpoint is cleaner and matches the v1.3 `billing_routes.py` deliverable described in T4.

If Option B is chosen, T4 must be re-scoped to modify `routes/webhook_routes.py` instead of creating `routes/billing_routes.py`.

**🟢 COWORK DECISION (May 2026): Option A confirmed.** Dedicated endpoint at `POST /webhooks/stripe/billing` in new `routes/billing_routes.py`. New env var `STRIPE_BILLING_WEBHOOK_SECRET` for signature verification. Existing `routes/webhook_routes.py` (commerce/PSP webhooks) is untouched. Both endpoints write idempotency rows to the same `stripe_events` table — the table is shared infrastructure even though the endpoints are separate. This isolates failure domains: a billing webhook bug cannot affect commerce payment confirmations, and vice versa. It also matches the v1.3 deliverable structure ([`routes/billing_routes.py`](../) is the file path called out in Appendix D Week 1).

Stripe Dashboard configuration step (Jack to do once during setup): register the new endpoint URL `https://api.pivota.cc/webhooks/stripe/billing` and select only the billing-related event types (invoice.*, customer.subscription.*, checkout.session.completed). Copy the signing secret to the new env var. Existing commerce webhook registration unchanged.

---

## 🟢 Q2 status: closed (Codex implicit resolution matches Cowork decision)

Codex chose `merchant_id VARCHAR(50)` autonomously in T3 migrations — consistent with Option A. The decision above formalizes this. No re-prompt needed.

---

## Migration review items (from Wave 2)

### Item 1 — `net_attributed_gmv_cents GENERATED ALWAYS AS ... STORED` (migration 109)

**Status: Accept for v1.** STORED generated columns require a table rewrite on column add, which locks the table for the duration. For `commerce_attribution_edges`, which has production data, this is a real operational concern.

**Mitigation:** Since cohort #1 (20 brands) is not yet generating new attribution edges on the v1.3 pipeline, run migration 109 during off-hours (low traffic window) before activating any cohort #1 brand. Document the off-hours requirement in the deployment runbook (Week 9 deliverable). For future schema additions to this table, default to nullable + backfill pattern unless STORED-column semantics are explicitly required.

### Item 2 — UNIQUE index NULL-handling fix in migration 110

**Status: 🟢 Acknowledged. Good catch.** The `CREATE UNIQUE INDEX ... ON (..., COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))` expression index correctly handles the NULL-distinct issue. T6 (GMV aggregation) MUST match this expression in its ON CONFLICT clause — Codex working T6 needs to be made aware of this constraint when its prompt is dispatched. Suggest adding a note to T6's prompt:

> When writing `ON CONFLICT` for upserts into `gmv_attribution_daily`, you MUST match the expression index defined in migration 110: `ON CONFLICT (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))`. Standard `ON CONFLICT (date, merchant_id, agent_id, channel_partner_id)` will silently fail to dedupe NULL-keyed rows.

---

## Audit correction (no decision needed — for implementor awareness)

**T1 false positive — `settings.stripe_account_id` is not a missing config field.**

T1 noted: "`routes/agent_payout_management.py:199` references `settings.stripe_account_id`, but it is not defined in `config/settings.py`."

This is a false alarm. The `settings` variable at line 199 is the `PayoutSettingsRequest` request body parameter (defined at line 88 of that file), not the global `Settings` object from `config/settings.py`. `PayoutSettingsRequest.stripe_account_id` is correctly defined at line 35. No fix to `config/settings.py` is needed. T4 implementors should not add `stripe_account_id` to global settings based on this audit note.

---

## 🟢 Wave 3 dispatch status: UNBLOCKED

All four questions (Q1–Q4) resolved. T3 migration review items addressed. Claude Code may proceed to dispatch Wave 3 (T4 + T5 + T6 in parallel).

**Decisions summary, for the implementor's quick reference:**

| Q | Resolution | Affects |
|---|-----------|---------|
| Q1 | Use platform `STRIPE_SECRET_KEY` for all Billing/Invoicing/Connect | T4, T7, T8 |
| Q2 | `merchant_id VARCHAR(50)` (operational string, Codex's implicit choice) | T3 (closed) |
| Q3 | Fulfill on `checkout.session.completed`; `customer.subscription.updated` for ongoing sync; `customer.subscription.created` logged-only | T4 |
| Q4 | New dedicated endpoint `/webhooks/stripe/billing` in new `routes/billing_routes.py`; new env var `STRIPE_BILLING_WEBHOOK_SECRET`; shared `stripe_events` table | T4 |

**One T6 prompt addendum** (from Migration 076 review):
> T6's `ON CONFLICT` clause for `gmv_attribution_daily` upserts must match the expression index defined in migration 110: `ON CONFLICT (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))`. Standard column-list ON CONFLICT will silently fail to dedupe NULL-keyed rows.

**Migration 075 deployment note:** schedule during off-hours when cohort #1 traffic activates (table rewrite required by the STORED generated column). Add to Week 9 dry-run runbook.

No architectural changes to the v1.3 blueprint required from these decisions — all four resolutions are implementation specifics that fit within the existing architecture spec. No v1.4 needed.

---

## Wave 3 review: design gap identified (post-build)

After T6 landed, review caught that nothing in the current code populates `commerce_attribution_edges.gross_attributed_gmv_cents`. T6 reads from that column; without something stamping it, T7 will aggregate empty data and T8 will produce zero partner payouts.

**🟢 COWORK DECISION (May 2026): Add T9 — Stamp gross_attributed_gmv_cents in commerce payment flow.** Hook lives in `services/psp_payment_finalizer.py` (commerce-side, NOT in T4's billing_routes.py). Stamping runs after `finalize_payment_success` on any commerce order; gross = `subtotal_cents - discount_total_cents`. Idempotent via `WHERE gross_attributed_gmv_cents IS NULL`. Tax and shipping explicitly excluded.

T9 prompt at `docs/monetization/codex_dispatch/T9_attribution_stamping.md`. Dispatch in parallel with T7 + T8 in Wave 4.

No v1.4 blueprint change needed. T9 is the implementation of the existing v1.3 §1.3 GMV definition — just the piece that populates the upstream column. The column was already in the schema (migration 109); the stamping was implicit but not written.

---

## Wave 3 migration approvals

**🟢 Migration 083 (`metering_service_columns.sql`) — APPROVED.** Adds `credit_reservations.metadata` and `credit_ledger.source_type` that T5's write contract needs. Codex correctly identified these as gaps in T3's original schemas. Strictly additive, no operational concerns.

**🟢 Migration 084 (`invoice_payment_failed_status.sql`) — APPROVED.** Adds `invoices.paid_at` and expands the status CHECK constraint to include `payment_failed`. T4's invoice handlers write these states. Strictly additive.

Both can be applied during the standard migration window — no off-hours required (unlike 075 which rewrites the commerce_attribution_edges table).

---

## Wave 4 dispatch status: UNBLOCKED

Wave 4 = T7 (invoice generation) + T8 (partner settlement + Test Clock) + T9 (attribution stamping). All three are independent; dispatch in parallel.
