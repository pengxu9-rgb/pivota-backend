#!/usr/bin/env bash
# Provision the Tier B cart-link eligibility lane: ONE Cloud Run Job on the crawl subnet and its
# daily Cloud Scheduler trigger. Nothing else — no worker, no other job, no other trigger.
#
#   infra/gcp/setup_tierb_cart_link_eligibility_job.sh staging|prod <backend-tag>            # dark
#   infra/gcp/setup_tierb_cart_link_eligibility_job.sh staging|prod <backend-tag> --enable   # armed
#
# FOR THE OPERATOR. Not run by CI, not run by deploy_backend.sh. Runbook:
# docs/runbooks/tierb_cart_link_eligibility.md.
#
# WHY A JOB ON pivota-crawl, AND NEVER A services/audit_scheduler.py ENTRY. Every scheduler job
# runs on the `worker` service, whose egress is the default NAT (8.231.167.230) — the address
# payment partners allowlist. This job fetches ~40 merchant storefronts; NAT port exhaustion is
# per-IP, and ~50 requests over 37 Cloudflare-fronted domains in ~1 minute once tripped a
# cross-domain, IP-level 429 for ~15 minutes. So it leaves from pivota-crawl (NAT 34.82.199.35),
# exactly like the other crawl jobs (setup_scheduler.sh's mkcrawljob, the store-audit probes).
# The job ALSO paces itself (>= 1.5 s between request starts across all merchants, <= 3 merchants
# in flight, a 20-minute budget), because the crawl address is shared with those other jobs.
#
# DARK BY DEFAULT, ARMED ONLY BY --enable. One flag moves both switches together:
#   without --enable: TIERB_CART_LINK_ELIGIBILITY_ENABLED=false on the job, trigger PAUSED
#   with    --enable: TIERB_CART_LINK_ELIGIBILITY_ENABLED=true  on the job, trigger RESUMED
# Re-running WITHOUT --enable therefore disarms an armed lane — deliberately: the gate is set true
# only by someone who typed the flag on this run. The gate is also read INSIDE the job, so an
# execution started by hand against a dark job exits 0 having contacted nobody.
#
# THE SIDE EFFECT OF ARMING. Each run creates one abandoned Shopify checkout per merchant (two
# for a merchant whose first attempt hit a transport error), carrying our click id and NO buyer
# data. Nothing is paid.
#
# --max-retries 0: a failed execution is not re-run automatically. A re-run re-crawls every
# merchant (another round of checkouts, another burst on the crawl address); a human decides.
#
# SCHEDULE: 03:30 UTC daily. Neighbours on the crawl address: external-seed-destination-sweep
# (02:20, task-timeout 3600s, so done by 03:20) and the store-audit UCP probe (every 5 min,
# small). store-audit-ucp-reprobe-enqueue also fires at 03:30, but on the default subnet and
# without crawling (it only enqueues).
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
JOB=tierb-cart-link-eligibility
TRIGGER=tierb-cart-link-eligibility-cron
SCHEDULE="30 3 * * *"
SUBNET=pivota-crawl
# The worker identity, exactly as setup_scheduler.sh's crawl jobs and triggers use it: it already
# holds Secret Manager access to DATABASE_URL and project-level run.invoker for Scheduler.
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
ENV_VARS="PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=$JOB,PIVOTA_COMMIT_SHA=$BACKEND_TAG"
ENV_VARS="$ENV_VARS,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2"
ENV_VARS="$ENV_VARS,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600"
ENV_VARS="$ENV_VARS,TIERB_CART_LINK_ELIGIBILITY_ENABLED=$ENABLED"

echo "== job: $JOB (subnet $SUBNET, gate $ENABLED)"
verb=create; have "$GCLOUD" run jobs describe "$JOB" --region "$REGION" && verb=update
# task-timeout 1800s: the job's own budget is 1200s; the rest is headroom for in-flight requests
# (30 s timeout each) and the DB writes, so the budget — not Cloud Run — is what stops a run.
"$GCLOUD" run jobs "$verb" "$JOB" --region "$REGION" --image "$BACKEND_IMAGE" --service-account "$SA" \
  --network default --subnet "$SUBNET" --vpc-egress all-traffic \
  --max-retries 0 --task-timeout 1800s --cpu 1 --memory 1Gi \
  --labels "env=$ENV,managed-by=infra-gcp,lane=tierb-cart-link" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "$ENV_VARS" \
  --command python --args "-m,jobs.tierb_cart_link_eligibility,--budget-seconds,1200" --quiet

echo "== trigger: $TRIGGER ($SCHEDULE UTC)"
URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run"
verb=create; have "$GCLOUD" scheduler jobs describe "$TRIGGER" --location "$REGION" && verb=update
"$GCLOUD" scheduler jobs "$verb" http "$TRIGGER" --location "$REGION" \
  --schedule="$SCHEDULE" --time-zone=Etc/UTC --uri="$URI" --http-method=POST \
  --oauth-service-account-email="$SA" --attempt-deadline=300s --quiet

if [ "$ENABLED" = true ]; then
  "$GCLOUD" scheduler jobs resume "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to resume $TRIGGER" >&2; exit 1; }
  echo "ARMED: $JOB runs daily at 03:30 UTC with the gate on."
else
  "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to pause $TRIGGER - it may be LIVE" >&2; exit 1; }
  echo "DARK: $TRIGGER is paused and the job's gate is false. Arm with --enable."
fi
