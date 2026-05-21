# 01 — Stripe webhook signature mismatch

**Authorization:** read-only investigation; rotation requires platform admin.

## Symptom

`POST /webhooks/stripe/billing` returns `400 Invalid signature`. Stripe Dashboard → Developers → Webhooks shows recent attempts failing. Billing events (subscription updates, invoice paid) stop flowing to the local mirror.

## Investigation

1. Confirm the endpoint is the right one. Stripe sends to `https://api.pivota.cc/webhooks/stripe/billing` (NOT `/webhooks/stripe`, which is commerce).
2. Check the secret currently configured in Stripe Dashboard for that endpoint matches Railway's `STRIPE_BILLING_WEBHOOK_SECRET`:
   ```bash
   railway variables --json --environment production --service web | jq -r '.STRIPE_BILLING_WEBHOOK_SECRET' | head -c 8
   ```
   Compare the first 8 chars with the secret shown in Stripe Dashboard (also `whsec_` + 8 visible chars).
3. Check whether any successful events landed since the secret was last touched:
   ```sql
   SELECT MAX(received_at) FROM stripe_events;
   ```
   If `received_at` predates the last secret rotation, the deployed secret is stale.

## Resolution

- **Secret stale:** rotate via Stripe Dashboard → Webhooks → reveal new signing secret → update Railway env var → trigger redeploy. Stripe automatically replays recent failures.
- **Endpoint misregistered:** verify the URL in Stripe Dashboard. The billing endpoint must point to `/webhooks/stripe/billing` and subscribe ONLY to `invoice.*`, `customer.subscription.*`, `checkout.session.completed`.
- **Signature header missing entirely** (rare; bad client): check `routes/billing_routes.py` log for `Unable to extract timestamp and signatures from header` — this means the request didn't carry `Stripe-Signature`. Block at the WAF/edge if it's not from Stripe IPs.

## Prevention

- Bind `STRIPE_BILLING_WEBHOOK_SECRET` rotation to a Railway redeploy checklist; never rotate without confirming the env var lands before Stripe expires the old secret.
- Subscribe a low-priority Slack alert on `routes/billing_routes.py` ERROR `Invalid Stripe billing webhook signature` — if more than 5 in 10 minutes, page the on-call.
