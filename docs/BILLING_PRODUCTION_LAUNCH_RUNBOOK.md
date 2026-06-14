# Billing — Production Launch Runbook

Bring the merchant self-serve billing system live. Code is merged
(backend: JWT auth on `/api/billing/*` + `GET /api/billing/plans`;
frontend: `/dashboard/billing`). What remains is operational config.

**Owner:** _<fill in>_   **Target window:** _<fill in>_
**Rollback:** see [§6](#6-rollback--kill-switch). Nothing here is destructive
except the optional plan edits in §1; each step is independently reversible.

## At a glance — what to do

| # | Step | Touches | Reversible? |
|---|------|---------|-------------|
| 1 | Verify live subscription plans | prod DB / Stripe Live | edits only |
| 2 | Wire the Stripe **billing** webhook | Stripe Dashboard + env | yes |
| 3 | Confirm pilot merchant `contact_email` | prod DB | yes |
| 4 | Decide `PARTNER_REV_SHARE_USE_V2` | env | yes |
| 5 | End-to-end smoke test | live (test card) | n/a |

Key facts the system relies on:
- The portal calls `GET /api/billing/plans`, which returns only rows whose
  `stripe_mode` matches the platform key (`sk_live…` → `live`). So **the prod
  app must run with the Stripe Live key**, and live plan rows must exist.
- Webhook lands at **`POST /webhooks/stripe/billing`** (distinct from the
  commerce-payments webhook at `/webhooks/stripe`).
- Auth: the portal authenticates with the merchant JWT; only `approved`
  merchants pass the billing dependency.

---

## 1. Verify live subscription plans

Migration `124_subscription_plans_test_live_modes.sql` already seeds three live
rows (`ON CONFLICT DO NOTHING`), so on a migrated prod DB this is a **verify**,
not a create.

**1a. Confirm the live rows exist and look right:**
```sql
SELECT name, stripe_price_id, price_cents, monthly_credit_allowance, status, stripe_mode
FROM subscription_plans
WHERE stripe_mode = 'live' AND status = 'active'
ORDER BY tier_level;
```
Expect `starter`, `growth`, `scale`, each with a `price_1…` id, `price_cents > 0`.

> ⚠️ **Allowance check.** Migration 124 seeds allowances `1000 / 5000 / 25000`,
> while the true-up migration sets `4000 / 18000 / 75000`. Whichever applied
> last wins — confirm the values above are the intended ones for launch and
> reconcile if not (`UPDATE subscription_plans SET monthly_credit_allowance = …
> WHERE name = … AND stripe_mode = 'live';`).

**1b. Confirm those `stripe_price_id`s exist in the Stripe *Live* account**
(Dashboard → Products, in Live mode). A row pointing at a non-existent or
test-mode price will 404 / fail at checkout.

**1c. Verify the endpoint returns them** (with a real approved-merchant JWT):
```bash
curl -s https://api.pivota.cc/api/billing/plans \
  -H "Authorization: Bearer <MERCHANT_JWT>" | jq
# → { "plans": [ { "name":"starter","price_id":"price_1…","price_cents":9900,
#                  "monthly_credit_allowance":…,"currency":"usd" }, … ] }
```
Empty `plans` → the app isn't on the Live key, or no live rows exist. Re-check 1a/1b.

---

## 2. Wire the Stripe billing webhook

**2a. Confirm env (prod web service):**
- `STRIPE_SECRET_KEY` = the **Live** secret key (`sk_live_…`).
- `STRIPE_BILLING_WEBHOOK_SECRET` — set in 2c below. (This is **separate** from
  `STRIPE_WEBHOOK_SECRET`, which is the commerce-payments webhook.)

**2b. Create the endpoint** — Stripe Dashboard (Live mode) → Developers →
Webhooks → Add endpoint:
- URL: `https://api.pivota.cc/webhooks/stripe/billing`
- Events:
  - `checkout.session.completed`
  - `customer.subscription.created`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.paid`
  - `invoice.payment_failed`
  - `payment_intent.succeeded`

**2c. Copy the signing secret** (`whsec_…`) → set `STRIPE_BILLING_WEBHOOK_SECRET`
in prod → redeploy/restart so it loads.

**2d. Smoke the endpoint:** in the Stripe webhook page, "Send test event" for
`invoice.paid`. Expect **HTTP 200**. (Handler returns 400 if the secret is unset
or signature mismatches — that means 2a/2c is wrong.)

---

## 3. Confirm merchant `contact_email` (risk R2)

The **first** upgrade for a merchant creates a Stripe customer from
`contact_email`; if it's missing, `POST /api/billing/checkout-session` returns
**400** and the upgrade fails.

**Check the pilot merchant(s):**
```sql
-- onboarding row (primary source the checkout flow reads)
SELECT merchant_id, status, contact_email
FROM merchant_onboarding
WHERE merchant_id = '<PILOT_MERCHANT_ID>';

-- merchants row (also consulted)
SELECT merchant_id, contact_email, stripe_customer_id
FROM merchants
WHERE merchant_id = '<PILOT_MERCHANT_ID>';
```
`status` must be `approved` (else billing returns 403). If `contact_email` is
null/empty, backfill it before the merchant tries to subscribe.

---

## 4. Decide `PARTNER_REV_SHARE_USE_V2`

Env flag (`config/settings.py`), default **`false`**. It does **not** affect the
merchant billing/checkout path — it gates the downstream **partner
settlement/payout** engine (v2 Stripe Connect pipeline vs legacy payouts).

- Leave **`false`** to launch merchant billing without changing payouts.
- Set **`true`** only when ops is ready for the v2 settlement pipeline
  (day-5 generate / day-10 transfer). Flipping it mid-cycle has settlement
  implications — coordinate with whoever owns partner payouts.

Decision: _<record here + who signed off>_.

---

## 5. End-to-end smoke test (live, test card on a sandbox merchant)

Prefer a **staging/sandbox merchant on Live keys** with a Stripe test card, or a
throwaway real charge you refund.

1. Log into the portal as an **approved** merchant → **Payments → Billing**.
2. Page loads: current plan/usage (or "No active plan"), plan cards populated
   from `/api/billing/plans`, statement history.
3. Click **Upgrade** on a plan → redirected to **Stripe Checkout**.
4. Complete payment → returns to `/dashboard/billing?status=success` → an
   **"Activating your plan…"** banner appears.
5. Within ~30s the banner clears and the plan flips to the new tier
   (webhook `checkout.session.completed` → `customer.subscription.*`).
   - If it times out to the soft "refresh shortly" message, check the Stripe
     webhook delivery log and the app logs for `/webhooks/stripe/billing`.
6. Next billing cycle (or via a backfilled statement) confirm a row appears in
   **Statement history**.
7. Confirm the **url-audit "Subscribe" CTA** now lands on the page (no 404).

---

## 6. Rollback / kill switch

- **Webhook:** disable the endpoint in Stripe (deliveries queue/retry; no data loss).
- **Plans:** `UPDATE subscription_plans SET status='archived' WHERE …` hides a
  plan from the upgrade list immediately (the endpoint only returns `active`).
- **UI:** the Billing tab is additive; remove the tab in `payments-nav.tsx` /
  the `/dashboard/billing` prefix in `merchant-navigation.ts` and redeploy to
  hide it. Existing subscriptions are unaffected.
- **Auth change:** the JWT path is additive to the API-key path; reverting the
  backend commit restores API-key-only behavior without affecting other routes.

---

## 7. Post-launch monitoring (first weeks)

- Stripe webhook **delivery success rate** for `/webhooks/stripe/billing`
  (retries, 4xx/5xx).
- App logs: `Orphan Stripe customer` (merchant row missing after customer
  create — provision + retry, idempotency key replays cleanly).
- 400s on `checkout-session` → almost always missing `contact_email` (see §3).
- Activation-poll timeouts in the UI → webhook lag or misconfig.

---

### Appendix — surface reference
- `GET  /api/billing/me/current-period` — plan + usage snapshot (JWT or API key)
- `GET  /api/billing/me/statements?limit=12` — frozen/invoiced history
- `GET  /api/billing/plans` — active plans for the platform's Stripe mode
- `POST /api/billing/checkout-session` — `{price_id, success_url, cancel_url}` → `{session_url, session_id}`
- `POST /webhooks/stripe/billing` — Stripe billing events
- Env: `STRIPE_SECRET_KEY`, `STRIPE_BILLING_WEBHOOK_SECRET`, `PARTNER_REV_SHARE_USE_V2`
