#!/usr/bin/env bash
# Provision the merchant purchasability sweep: ONE Cloud Run Job on the crawl subnet and its
# hourly Cloud Scheduler trigger. Nothing else — no worker, no other job, no other trigger.
#
#   infra/gcp/setup_merchant_purchasability_sweep_job.sh staging|prod <backend-tag>            # dark
#   infra/gcp/setup_merchant_purchasability_sweep_job.sh staging|prod <backend-tag> --enable   # armed
#
# FOR THE OPERATOR. Not run by CI, not run by deploy_backend.sh — and a backend deploy never
# re-images this job: it runs <backend-tag> until this script is re-run with a newer one. Runbook:
# docs/runbooks/merchant_purchasability.md §9.
#
# WHY A JOB ON pivota-crawl, AND NEVER A services/audit_scheduler.py ENTRY AGAIN. Until 2026-09-27
# the sweep was an interval job on the `worker` service. Its egress is the default NAT
# (8.231.167.230) — the address payment partners allowlist — and every check renders a merchant
# checkout; NAT port exhaustion is per-IP, and ~50 requests over 37 Cloudflare-fronted domains in
# ~1 minute once tripped a cross-domain, IP-level 429 for ~15 minutes (see
# setup_tierb_cart_link_eligibility_job.sh). And every on-merge worker deploy restarted the
# interval clock: on 2026-09-27 the 3600 s tick was pre-empted three times (worker revisions 02:10,
# 04:03, 04:21) and one report landed in four hours. A Cloud Scheduler cron is reset by nothing.
# So it leaves from pivota-crawl (NAT 34.82.199.35), like every other storefront crawler. The job
# also paces itself: request starts >= 1.5 s apart across the whole run (the Tier B job's pacer),
# one merchant at a time, a 600 s budget.
#
# VANTAGE. The job records its checks under vantage `worker`, the name the facts already carry and
# the one prod `web` reads (MERCHANT_PURCHASABILITY_BUYER_VANTAGE unset). Since this job exists,
# "worker" means OUR CRAWL EGRESS. The reasoning is in jobs/merchant_purchasability_sweep.py.
#
# DARK BY DEFAULT, ARMED ONLY BY --enable. One flag moves both switches together:
#   without --enable: MERCHANT_PURCHASABILITY_SWEEP_ENABLED=false on the job, trigger PAUSED
#   with    --enable: MERCHANT_PURCHASABILITY_SWEEP_ENABLED=true  on the job, trigger RESUMED
# Re-running WITHOUT --enable therefore disarms an armed lane — deliberately: the gate is set true
# only by someone who typed the flag on this run. The gate is also read INSIDE the job, so an
# execution started by hand against a dark job exits 0 having contacted nobody.
#
# THE SIDE EFFECT OF ARMING. Each run creates one abandoned Shopify checkout per merchant it
# checks (at most MERCHANT_PURCHASABILITY_BATCH), carrying a throwaway click id (clk_mpx_...) and
# NO buyer data. Nothing is paid.
#
# --max-retries 0: a failed execution is not re-run automatically. A re-run re-checks every
# merchant (another round of checkouts, another burst on the crawl address); the next hourly run
# is soon enough, or a human decides.
#
# SCHEDULE: minute 07 of every hour EXCEPT 02, 03 and 04 UTC (21 runs a day). Neighbours on the
# crawl address, each as its LONGEST possible window (task timeout x (max-retries + 1)):
#   * store-audit-ucp-probe / store-audit-commerce-probe  every 5 min (:00, :05, ...), small;
#   * retailer-ingest-drain                               every 10 min (:00, :10, ...), stages of
#                                                         up to ~60 min, so no minute is drain-free;
#   * external-seed-destination-sweep                     02:20 daily, task-timeout 3600s AND
#                                                         mkcrawljob's --max-retries 1 -> 04:20;
#   * tierb-cart-link-eligibility                         03:30 daily, task-timeout 1800s, no
#                                                         retry -> 04:00; it checks THE SAME merchants.
# :07 is not a multiple of 5, so a run never starts on the same minute as the probe and drain
# bursts. A run is over by :27 at the latest (task-timeout 1200s), so 01:07 ends before 02:20 and
# 05:07 starts after the external-seed sweep's retry could have ended; 02:07, 03:07 and 04:07 would
# each share the address with a daily crawl. The 01:07 -> 05:07 gap is four hours against a 72 h
# fact TTL. tests/test_setup_merchant_purchasability_sweep_job.py re-derives these windows from the
# neighbours' own scripts, retries included.
# Two scheduled executions of THIS job can never overlap: task-timeout 1200s < the 3600s spacing
# (Cloud Run has no max-instances for jobs; the spacing is what stands in for the scheduler's old
# max_instances=1). A HAND execution can overlap a scheduled one — see the runbook before running
# `gcloud run jobs execute` within 20 minutes of :07.
set -euo pipefail

ENV="${1:-}"; BACKEND_TAG="${2:-}"; FLAG="${3:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <backend-tag> [--enable]" >&2; exit 2 ;;
esac
[ -n "$BACKEND_TAG" ] || { echo "backend-tag is required" >&2; exit 2; }
case "$FLAG" in
  "") ENABLED=false ;;
  --enable) ENABLED=true ;;
  *) echo "unknown argument '$FLAG' (the only option is --enable)" >&2; exit 2 ;;
esac
[ "$#" -le 3 ] || { echo "too many arguments" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; SHARED=pivota-shared
JOB=merchant-purchasability-sweep
TRIGGER=merchant-purchasability-sweep-cron
SCHEDULE="7 0-1,5-23 * * *"
SUBNET=pivota-crawl
# The sweep's own dials, PINNED here because the task timeout below is derived from them. A re-run
# of this script resets them; tune them here, not with `gcloud run jobs update`.
BUDGET_SECONDS=600
BATCH=20
# The worker identity, exactly as setup_scheduler.sh's crawl jobs and the Tier B job use it: it
# already holds Secret Manager access to DATABASE_URL and project-level run.invoker for Scheduler.
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
BACKEND_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/backend:$BACKEND_TAG"
have(){ "$@" >/dev/null 2>&1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"

have "$GCLOUD" iam service-accounts describe "$SA" || { echo "missing $SA" >&2; exit 1; }
have "$GCLOUD" artifacts docker images describe "$BACKEND_IMAGE" \
  || { echo "missing image $BACKEND_IMAGE; build it first" >&2; exit 1; }
# The crawl subnet must exist, or --subnet would fail late; and it must be the crawl NAT's.
have "$GCLOUD" compute networks subnets describe "$SUBNET" --region "$REGION" \
  || { echo "missing subnet $SUBNET; run setup_crawl_egress.sh first" >&2; exit 1; }

# PIVOTA_ENV is required (a Job inherits nothing; see scripts/ops/run_oneoff_job.sh), and the two
# DB timeouts re-supply the guardrails `web` has — db/database.py defaults both to OFF.
# VANTAGE_PROXY_URL is deliberately NOT set: a second vantage doubles every merchant's checks, and
# the task timeout below is sized for one.
ENV_VARS="PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=$JOB,PIVOTA_COMMIT_SHA=$BACKEND_TAG"
ENV_VARS="$ENV_VARS,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2"
ENV_VARS="$ENV_VARS,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600"
ENV_VARS="$ENV_VARS,MERCHANT_PURCHASABILITY_BUDGET_SECONDS=$BUDGET_SECONDS"
ENV_VARS="$ENV_VARS,MERCHANT_PURCHASABILITY_BATCH=$BATCH"
ENV_VARS="$ENV_VARS,MERCHANT_PURCHASABILITY_SWEEP_ENABLED=$ENABLED"
# THE SHARED SHOPIFY-EDGE PACER (#2474), exactly as the Reap cart-proof setup script sets it on the
# Reap proof jobs: every request (all to Shopify stores) also takes a slot of the aggregate budget
# every crawl job on the crawl IP shares, and the lease cap of 2 bounds what this job can ever
# hold of that shared schedule. CRAWL_SHOPIFY_EDGE_RPS is deliberately NOT set: every job on the
# address must use the same shared rate. The IP breaker's dials
# (MERCHANT_PURCHASABILITY_IP_THROTTLE_*) are NOT set either: their defaults are the ones the
# module documents (MERCHANT_PURCHASABILITY_IP_THROTTLE_TRIP_HOSTS=0 disarms it).
ENV_VARS="$ENV_VARS,CRAWL_SHOPIFY_EDGE_PACER_ENABLED=true,CRAWL_SHOPIFY_EDGE_LEASE=2"

echo "== job: $JOB (subnet $SUBNET, gate $ENABLED)"
verb=create; have "$GCLOUD" run jobs describe "$JOB" --region "$REGION" && verb=update
# task-timeout 1200s = the 600 s budget + 600 s for the ONE merchant a budget cannot stop (the
# budget stops the job STARTING a merchant; one in flight runs to completion). That merchant's
# realistic worst case is ~300 s — the preflight returns at its first transport failure, and a
# store answers a catalog page or two plus 2-3 permalink hops, now >= 1.5 s apart — which is what
# the old scheduler deadline (600 + 300 = 900) allowed; this doubles it, so the budget, not Cloud
# Run, is what stops a run on anything but a pathological store. It is NOT a bound: a store could
# in principle make every one of 20 catalog pages and the permalink redirect up to 10 times, at
# 30 s a request. Such a run is killed at 1200 s, fails the execution, and strands nothing. A cut run strands nothing: each fact is one upsert,
# written as it goes, and the next hour starts with whoever was not reached.
# --args= in the EQUALS form: the value starts with a dash, and `--args "-m,..."` is parsed by
# gcloud as a second flag ("argument --args: expected one argument") — #2367, the Tier B job's
# first prod run.
"$GCLOUD" run jobs "$verb" "$JOB" --region "$REGION" --image "$BACKEND_IMAGE" --service-account "$SA" \
  --network default --subnet "$SUBNET" --vpc-egress all-traffic \
  --max-retries 0 --task-timeout 1200s --cpu 1 --memory 1Gi \
  --labels "env=$ENV,managed-by=infra-gcp,lane=merchant-purchasability" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "$ENV_VARS" \
  --command python --args="-m,jobs.merchant_purchasability_sweep" --quiet

echo "== trigger: $TRIGGER ($SCHEDULE UTC)"
URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run"
verb=create; have "$GCLOUD" scheduler jobs describe "$TRIGGER" --location "$REGION" && verb=update
"$GCLOUD" scheduler jobs "$verb" http "$TRIGGER" --location "$REGION" \
  --schedule="$SCHEDULE" --time-zone=Etc/UTC --uri="$URI" --http-method=POST \
  --oauth-service-account-email="$SA" --attempt-deadline=300s --quiet

if [ "$ENABLED" = true ]; then
  "$GCLOUD" scheduler jobs resume "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to resume $TRIGGER" >&2; exit 1; }
  echo "ARMED: $JOB runs at :07 past every hour except 02, 03 and 04 UTC, with the gate on."
else
  "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to pause $TRIGGER - it may be LIVE" >&2; exit 1; }
  echo "DARK: $TRIGGER is paused and the job's gate is false. Arm with --enable."
fi
