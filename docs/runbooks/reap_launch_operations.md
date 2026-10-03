# Reap launch operations preparation

This is a preparation plan. No provisioning, merchant fetch, proof write,
notification, deployment, issuer registration or production arming ran in this
review. Observations below are metadata only, from 2 October 2026 at about
07:17 Asia/Shanghai.

## Current inventory and evidence boundaries

Both `pivota-staging` and `pivota-prod` lack the named Cloud Run job
`reap-cart-proof-mirror` and Scheduler job `reap-cart-proof-mirror-cron`.
Monitoring API lists contain zero Reap-named policies in each project and no
unread next page. This does not prove metric absence or alert delivery.

Production gateway serves image `0b5a17c18` (revision `gateway-00477-qib`);
staging gateway serves `873b607` (`gateway-00020-jok`). Both UCP profiles still
advertise production's commerce host. See the gateway endpoint readiness
runbook before the staging rehearsal.

The private `prod_reap_readiness.sh` runs read-only SQL through a newly created
one-off Cloud Run job. It is an infrastructure write and must not be presented
as a read-only inventory command. The old agent/candidate/freshness census has
not been refreshed; no buyer or secret rows were exported here. Obtain reviewed
counts/selected readiness facts via an existing authorized admin/DB path or
an explicitly approved one-off job. Do not log database URLs, tokens, buyer
details or full purchase rows.

## Monitoring rollout after code review

`setup_monitoring.sh` must be reviewed/merged first. The environment correction
preserves production names/payloads, including its three Reap alerts, and uses
`staging:` policy names and `pivota staging alerts` in staging. Staging creates
no uptime checks against production's six hostnames and no empty host/TLS
policies. Both ordinary and new-metric policy write/retry paths use the same
environment naming rule. Existing misnamed staging objects are not silently
deleted; inspect their targets/bindings and explicitly retire or rename them.

Before executing the script, settle the monitored destination and owner. Prod
requires `ALERT_EMAIL`; an unverified channel makes the script fail. Test
delivery using an agreed nonpayment synthetic condition after setup approval;
do not equate installed policy count with a delivered page. Read back project,
policy enabled state, metric filters, worker resource, timing and notification
channel verification. Confirm stuck/failing/silent alerts on the selected
worker. The silence alert cannot detect a never-armed rail and ages out after
24 hours without reports; retain scheduler-health and explicit start checks.

## First proof job provisioning plan

Preparation can continue while merchant fetching is gated. Before any actual
merchant request, require crawl-controller confirmation that the IP incident
has remained closed for at least 24 hours and Peng's explicit go. Re-read
actual schedules and executions at that time; do not treat the sample October
2 run time as clearance.

1. Select the exact approved backend image/digest. Confirm it contains both
   the pacer and breaker; `f2ec348` is an ancestor of audited backend `ade2e5a`.
   Use a release image containing the reviewed recovery fixes for rehearsal.
2. Review a mirror-only Cloud Run job definition, persist scope
   `--only judydoll.com`, apply=false, crawl subnet/egress, pacer enabled,
   lease=2, no `CRAWL_SHOPIFY_EDGE_RPS`, bounded task/budget, max-retries=0,
   service account and the correct environment's database secret binding.
   No credentials appear in the release manifest.
3. Prefer no Scheduler trigger for the first controlled run. The existing
   `setup_reap_cart_proof_jobs.sh` provisions BOTH writers and full-scope
   defaults; do not execute it unchanged for a mirror-only pilot. A newly
   created Scheduler trigger starts enabled before its subsequent pause,
   another reason to omit it for this one-off phase. Any future triggers
   require reviewed dark defaults and immediate paused-state readback.
4. After incident clearance, run one staging dry run. Even apply=false fetches
   merchant storefronts and exercises shared pacer/lease state; "dark" does
   not mean read-only or inert when manually executed. Correlate logs to the
   exact execution ID. Require clean exit, intended domain, pacer grants,
   no IP throttle/block burst and a valid proof candidate for the approved
   SKU/variant/quantity/currency/URL.
5. Review the target proof, then an explicitly approved one-execution apply.
   Re-read stored proof content and observation/expiry times, not just counts;
   dry/apply counts can legitimately differ. Keep persistent apply=false and
   no recurring trigger. Redo the complete sandbox rehearsal.
6. Run production dry run only after staging acceptance. Apply the exact proof
   near the controlled purchase once payment/support/identity gates are met.
   Defer enrichment, full-store work and recurring enablement.

Stop on 429/throttle, blocked/non-JSON/dead-handle target, bad host/variant,
wrong environment, stale proof or unreadable pacing. Do not restamp observation
timestamps to manufacture freshness. Sandbox fixtures must carry explicit
simulation provenance and enforce the reviewed environment/host/agent/domain/
market guard; source observation times remain unchanged.

## Rollback and temporary trust removal

Pause NEW creates, then continue owner GET and authoritative reconciliation
for every exposed/uncertain checkout. The existing gateway/backend master
flags gate reads as well as creates, and the worker's master flag stops its
poller; leaving these on while draining is essential. Release must include
tested create-only gates on gateway AND backend and an enforceable pilot
agent/merchant/market scope, not just a dedicated key.

Reconcile unknown attempts using their original owner/request key. The buyer
must retain that attempt and cannot start a fresh key simply because a pause
is enabled. Record provider, purchase, order, consent, amount/currency, one
claim/attribution edge and correct converted-event agent before removing trust.

Then remove the temporary issuer/key and local private credentials; keep the
authenticated support/read path. Gateway binding refresh defaults to 60s;
during registry failure a cached binding may remain accepted for up to 15m.
Prove actual old-token refusal on serving revisions after refresh/expiry and
under a controlled registry-failure check. Deleting a JWKS document or binding
does not instantly clear every verifier/cache.
