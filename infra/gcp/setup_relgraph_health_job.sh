#!/usr/bin/env bash
# Provision the relationship graph's daily health + nightly readout lane: ONE Cloud Run Job
# (`relgraph-health`, gateway image) and its daily Cloud Scheduler trigger. Nothing else.
#
#   infra/gcp/setup_relgraph_health_job.sh staging|prod <gateway-tag>            # dark
#   infra/gcp/setup_relgraph_health_job.sh staging|prod <gateway-tag> --enable   # armed
#
# FOR THE OPERATOR. Not run by CI or by any deploy script. Runbook: docs/runbooks/catalog_jobs_on_gcp.md.
#
# WHAT IT RUNS. PIVOTA-Agent `npm run relgraph:health-job` (scripts/run-relgraph-health-job.js):
#   - the serving guard (0 suppressed rows / 0%, the three critical reasons),
#   - the expiry alarm (>30% of serving edges expiring in 14 days, or fewer than 500 rows),
#   - the no-op detector (runs passing while nothing applies; reports, does not page),
#   - and, REPORT-ONLY, the nightly readout: the last week of relgraph-sync runs, the review-error
#     quarantine and the review pool, in counts. Those were read by hand from a laptop after every
#     nightly; this job prints them as one `jsonPayload.message="relgraph_nightly_readout"` line.
# READ-ONLY: it issues SELECTs only. Exit 1 = a threshold breached (fails the execution, pages via
# the "prod: Cloud Run job failing" policy); exit 2 = the job itself failed.
#
# THE GATEWAY TAG must carry the readout (PIVOTA-Agent ops/relgraph-readout-and-image-build) for the
# readout line to appear; an older tag that has the health job still runs the three checks.
#
# WHY THIS FILE AND NOT setup_scheduler.sh. setup_scheduler.sh defined this job and a 10:00 trigger
# since 2026-09-09, but running that script reconciles EVERY job in it onto the tags you pass, so
# nobody ran it and the job never existed. A lane that cannot be provisioned on its own is not
# provisioned; the definition moved here and setup_scheduler.sh no longer carries it.
#
# DARK BY DEFAULT, ARMED ONLY BY --enable. Without --enable the trigger is PAUSED; with it, RESUMED.
# Re-running WITHOUT --enable therefore disarms an armed lane, deliberately, and it pauses an existing
# trigger FIRST, before any preflight, so a missing image cannot stop a disarm. The job is read-only,
# so an execution started by hand against a dark job is harmless. Fastest disarm, no script needed:
#   gcloud scheduler jobs pause relgraph-health-cron --location us-west1 --project pivota-prod
#
# THIS SCRIPT OWNS THE JOB'S WHOLE ENV AND SECRETS. The update path uses --set-env-vars and
# --set-secrets, which REPLACE the set: a value added by hand (RELGRAPH_FAIL_ON_NOOP=true, a
# RELGRAPH_CRITICAL_REASONS override) is wiped by the next run of this script. Put it here instead.
#
# SCHEDULE: 17:05 UTC daily. relgraph-sync starts at 10:37 with a 21600s task timeout, so a slow night
# can still be writing until 16:37; a health check read in the middle of that could report a
# half-applied graph. 17:05 is after the latest possible end, and outside the 10:15-11:45 window
# reserved for relgraph-sync on pivota-pg. Default subnet: it reaches only Cloud SQL, never a merchant
# host, so the crawl address is not involved. Neighbours: external-referral-refresh (16:15 and 18:15,
# crawl subnet, ~55 min each).
set -euo pipefail

ENV="${1:-}"; GATEWAY_TAG="${2:-}"; FLAG="${3:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <gateway-tag> [--enable]" >&2; exit 2 ;;
esac
[ -n "$GATEWAY_TAG" ] || { echo "gateway-tag is required" >&2; exit 2; }
case "$GATEWAY_TAG" in
  latest) echo "refusing the floating tag 'latest' - pass the commit sha, so PIVOTA_COMMIT_SHA names a real commit" >&2; exit 2 ;;
esac
case "$FLAG" in
  "") ENABLED=false ;;
  --enable) ENABLED=true ;;
  *) echo "unknown argument '$FLAG' (the only option is --enable)" >&2; exit 2 ;;
esac
[ "$#" -le 3 ] || { echo "too many arguments" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1
JOB=relgraph-health
TRIGGER=relgraph-health-cron
SCHEDULE="5 17 * * *"
# The worker identity, as setup_scheduler.sh's gateway jobs use it: it already holds Secret Manager
# access to DATABASE_URL_NOVERIFY and project-level run.invoker for Scheduler.
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
have(){ "$@" >/dev/null 2>&1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"
# shellcheck source=infra/gcp/_gateway_image.sh
. "$(cd "$(dirname "$0")" && pwd)/_gateway_image.sh"

# DISARM FIRST: a run without --enable pauses an existing trigger before anything below can refuse.
if [ "$ENABLED" != true ] && have "$GCLOUD" scheduler jobs describe "$TRIGGER" --location "$REGION"; then
  "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to pause $TRIGGER - it may be LIVE" >&2; exit 1; }
  echo "== paused $TRIGGER (disarm runs before the preflight)"
fi

have "$GCLOUD" iam service-accounts describe "$SA" || { echo "missing $SA" >&2; exit 1; }
GATEWAY_IMAGE="$(resolve_gateway_image "$GATEWAY_TAG")" \
  || { echo "missing image gateway:$GATEWAY_TAG in pivota-gateway and pivota; build it first (on merge to main by PIVOTA-Agent's build-gateway-image workflow, or by hand with infra/gcp/cloudbuild.gateway.yaml)" >&2; exit 1; }

# SUPPRESSED-ROW THRESHOLD: at most 1% AND at most 500 rows (owner's decision, 2026-11-10). The retired
# workflow's 0/0 was set for a smaller, cleaner graph. The first hand run here (2026-11-10) found 142
# suppressed rows (0.28% of 50,483), every one with a NON-critical reason (same-family variant,
# fragrance scent family, same product across sizes, fragrance category). The serving guard already
# hides those rows at read time, so 0/0 would page every night over a known, handled state. 1% / 500
# still pages on a ~3x jump. The three CRITICAL reasons stay at zero tolerance (below), and the
# expiry thresholds are unchanged.
# The expiry thresholds are the retired GitHub workflow's, exactly as setup_scheduler.sh passed them.
# RELGRAPH_CRITICAL_REASONS is deliberately NOT passed: gcloud splits --set-env-vars on commas, and
# the job's parser falls back to its built-in three only when the list is EMPTY, so leaving it unset
# is the one path that cannot be mis-parsed into a single bogus reason. The three are:
#   ai_approved_dupe_quarantined
#   candidate_ref_unresolvable_nested_product_prefix
#   anchor_ref_unresolvable_nested_product_prefix
# RELGRAPH_FAIL_ON_NOOP is deliberately NOT set: the no-op detector reports and does not page.
ENV_VARS="PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=$JOB,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,DB_POOL_MAX=3"
ENV_VARS="$ENV_VARS,RELGRAPH_MARKET=US,RELGRAPH_MAX_SUPPRESSED_ROWS=500,RELGRAPH_MAX_SUPPRESSED_PCT=1"
ENV_VARS="$ENV_VARS,RELGRAPH_MAX_EXPIRING_14D_PCT=30,RELGRAPH_MIN_TOTAL_ROWS=500,RELGRAPH_READOUT_RUN_DAYS=7"

echo "== job: $JOB (default subnet, read-only)"
verb=create; have "$GCLOUD" run jobs describe "$JOB" --region "$REGION" && verb=update
# --max-retries 1: a read-only job; one retry absorbs a transient connection failure without a page.
"$GCLOUD" run jobs "$verb" "$JOB" --region "$REGION" --image "$GATEWAY_IMAGE" --service-account "$SA" \
  --network default --subnet default --vpc-egress all-traffic \
  --max-retries 1 --task-timeout 1800s --cpu 1 --memory 2Gi \
  --labels "env=$ENV,managed-by=infra-gcp,lane=relgraph" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "$ENV_VARS" \
  --command npm --args="run,relgraph:health-job" --quiet

echo "== trigger: $TRIGGER ($SCHEDULE UTC)"
URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run"
verb=create; have "$GCLOUD" scheduler jobs describe "$TRIGGER" --location "$REGION" && verb=update
"$GCLOUD" scheduler jobs "$verb" http "$TRIGGER" --location "$REGION" \
  --schedule="$SCHEDULE" --time-zone=Etc/UTC --uri="$URI" --http-method=POST \
  --oauth-service-account-email="$SA" --attempt-deadline=300s --quiet

if [ "$ENABLED" = true ]; then
  "$GCLOUD" scheduler jobs resume "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to resume $TRIGGER" >&2; exit 1; }
  echo "ARMED: $JOB runs daily at 17:05 UTC."
else
  "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to pause $TRIGGER - it may be LIVE" >&2; exit 1; }
  echo "DARK: $TRIGGER is paused. Arm with --enable."
fi
