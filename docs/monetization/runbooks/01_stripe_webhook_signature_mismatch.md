# 01 — Stripe webhook signature mismatch

> **Production is GCP Cloud Run (`pivota-prod`, `us-west1`), not Railway.** Rewritten 2026-08-25.
> See [operating_on_gcp_production.md](../../runbooks/operating_on_gcp_production.md).

**Authorization:** read-only investigation; rotation requires platform admin.

## Symptom

`POST /webhooks/stripe/billing` returns `400 Invalid signature`. Stripe Dashboard → Developers → Webhooks shows recent attempts failing. Billing events (subscription updates, invoice paid) stop flowing to the local mirror.

## Investigation

1. Confirm the endpoint is the right one. Stripe sends to `https://api.pivota.cc/webhooks/stripe/billing` (NOT `/webhooks/stripe`, which is commerce).
2. Check which secret **version** production is actually serving. On Cloud Run the value is a
   Secret Manager reference resolved at instance start, so the question "what is deployed" is
   answered by the reference and the revision, not by printing the value:
   ```bash
   gcloud run services describe web --project pivota-prod --region us-west1 --format=json \
     | python3 -c 'import json,sys; e=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env",[]); r=[x for x in e if x["name"]=="STRIPE_BILLING_WEBHOOK_SECRET"]; print(r[0].get("valueFrom",{}).get("secretKeyRef") if r else "ABSENT")'
   gcloud secrets versions list STRIPE_BILLING_WEBHOOK_SECRET --project pivota-prod \
     --format='table(name,state,createTime)'
   ```
   If the newest enabled version is newer than the serving revision's start time, the running
   instances are still on the OLD value — `:latest` resolves at instance start, not per request.
   Compare that timestamp against the last rotation in the Stripe Dashboard.

   **Do not print the secret into a terminal or a ticket.** The previous version of this step piped
   it through `head -c 8`; eight characters of a `whsec_` secret is still secret material in your
   shell history, and the version/timestamp comparison above answers the same question without it.
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
