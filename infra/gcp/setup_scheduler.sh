#!/usr/bin/env bash
# Replace Railway's cron + always-on workers with Cloud Run Jobs, Cloud Scheduler, and one
# single-instance worker service.
#   infra/gcp/setup_scheduler.sh staging|prod <backend-tag> <gateway-tag>
#
# THE SHAPE, and why it is not "one Scheduler job per cron":
#
#  1. FAST DRAINERS (audit/executor worker ticks, every 5-10s) CANNOT be Cloud Scheduler jobs -
#     Scheduler's minimum interval is 1 minute. They stay in-process, but on a DEDICATED service
#     pinned to exactly one instance (min=max=1), never on `web`. On Railway they ran inside the web
#     service, which is why every autoscaled Cloud Run instance would otherwise drain the same queue
#     concurrently (services/audit_scheduler.py has no cross-process lock; APScheduler's
#     max_instances=1 is per-process only).
#  2. PERIODIC BACKEND JOBS (daily_audit_check, nightly_index_health, outcome_aggregation_daily,
#     external_conversion_poll/15m, audit_health_tick/60m, catalog_onboard_queue_drain/30m,
#     store_lifecycle_reconciliation, audit_stability_canary) also live in that one worker process,
#     gated by AUDIT_WORKER_ENABLED. Moving each to Scheduler individually is a later refactor; the
#     single-worker shape reproduces Railway's semantics exactly, which is what a cutover needs.
#  3. TRUE CRON (relgraph-sync-routine, Railway schedule `37 10 * * *`) becomes a Cloud Run Job +
#     Cloud Scheduler trigger. It is a batch process that exits, which is what Jobs are for.
#  4. INVITATION WORKER (a bash `while true; sleep 60` loop on Railway) becomes a Cloud Run Job on a
#     1-minute schedule - the loop body is idempotent and the sleep IS the schedule.
#
# STAGING SAFETY: staging holds a restored PRODUCTION snapshot and still carries production
# third-party credentials. Every scheduled entity is therefore created PAUSED and the worker runs
# with AUDIT_WORKER_ENABLED=false in staging. Flipping them on in staging would execute
# production-derived queue rows against live Stripe/SendGrid/Shopify.
set -euo pipefail
ENV="${1:-}"; BACKEND_TAG="${2:-}"; GATEWAY_TAG="${3:-}"
[ -n "$ENV" ] && [ -n "$BACKEND_TAG" ] && [ -n "$GATEWAY_TAG" ] || { echo "usage: $0 staging|prod <backend-tag> <gateway-tag>" >&2; exit 2; }
case "$ENV" in
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production ;;
  *) exit 2 ;;
esac

# WORKERS/PAUSED are OPT-IN, exactly as in deploy_backend.sh, and for the same reason: until the DNS
# flip, GCP prod runs against a COPY of production data with the REAL production credentials while
# Railway prod is still serving. Deriving these from $ENV is what makes `setup_scheduler.sh prod`
# quietly arm a second set of drainers over the same queue — duplicate emails, duplicate settlement
# attempts, duplicate captures. It did exactly that on 2026-08-20 (caught in under a minute, no
# executions fired) because deploy_backend.sh had been made opt-in and this script had not.
#
#   pre-cutover:  infra/gcp/setup_scheduler.sh prod <a> <b>                  (inert: default)
#   at cutover:   WORKERS=true PAUSED=0 infra/gcp/setup_scheduler.sh prod <a> <b>
: "${WORKERS:=false}"
: "${PAUSED:=1}"
case "$WORKERS" in true|false) ;; *) echo "WORKERS must be exactly true or false (got '$WORKERS')" >&2; exit 2 ;; esac
case "$PAUSED"  in 0|1)         ;; *) echo "PAUSED must be exactly 0 or 1 (got '$PAUSED')" >&2; exit 2 ;; esac
GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; HERE="$(cd "$(dirname "$0")" && pwd)"
export CLOUDSDK_CORE_PROJECT="$PROJECT"
BACKEND_IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/backend:$BACKEND_TAG"
GATEWAY_IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/gateway:$GATEWAY_TAG"
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
have(){ "$@" >/dev/null 2>&1; }

# ---------------------------------------------------------------- 1. the single-instance worker
ENV_FILE="$HERE/env.$ENV.yaml"; SECRETS_FILE="$HERE/secrets.$ENV.list"
[ -f "$ENV_FILE" ] && [ -f "$SECRETS_FILE" ] || { echo "missing $ENV_FILE / $SECRETS_FILE - run port_railway_env.py first" >&2; exit 1; }
MERGED=$(mktemp); chmod 600 "$MERGED"; trap 'rm -f "$MERGED"' EXIT INT TERM
# Drop keys we are about to set ourselves: the ported file may already carry them from the
# overrides, and a duplicate YAML key is a warning today and an error in a future gcloud.
grep -vE '^(PIVOTA_ENV|PIVOTA_SERVICE_NAME|PIVOTA_COMMIT_SHA|SKIP_HEAVY_STARTUP_INIT|AUDIT_WORKER_ENABLED|REVIEWS_INVITATION_WORKER_ENABLED|DB_POOL_MIN_SIZE|DB_POOL_MAX_SIZE):' "$ENV_FILE" > "$MERGED"
{ printf 'PIVOTA_ENV: "%s"\nPIVOTA_SERVICE_NAME: "worker"\nPIVOTA_COMMIT_SHA: "%s"\n' "$PIVOTA_ENV" "$BACKEND_TAG"
  printf 'SKIP_HEAVY_STARTUP_INIT: "true"\n'
  printf 'AUDIT_WORKER_ENABLED: "%s"\nREVIEWS_INVITATION_WORKER_ENABLED: "%s"\n' "$WORKERS" "$WORKERS"
  printf 'DB_POOL_MIN_SIZE: "2"\nDB_POOL_MAX_SIZE: "10"\n'
} >> "$MERGED"
echo "== worker service (min=max=1, ingress internal)"
"$GCLOUD" run deploy worker --region "$REGION" --image "$BACKEND_IMAGE" --service-account "$SA" \
  --network default --subnet default --vpc-egress all-traffic \
  --env-vars-file "$MERGED" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")" \
  --port 8080 --cpu 1 --memory 2Gi --concurrency 1 \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling --execution-environment gen2 --ingress internal \
  --labels "env=$ENV,service=worker,managed-by=infra-gcp" --quiet

# ---------------------------------------------------------------- 2. Cloud Run Jobs
mkjob(){ # name image sa command...
  local name="$1" image="$2"; shift 2
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$SA" \
    --network default --subnet default --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 3600s --cpu 1 --memory 2Gi \
    --labels "env=$ENV,managed-by=infra-gcp" --quiet "$@"
}
echo "== job: relgraph-sync (Railway cron 37 10 * * *)"
mkjob relgraph-sync "$GATEWAY_IMAGE" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=relgraph-sync,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,DB_POOL_MAX=3,PCI_KB_DB_POOL_MAX=1,INGREDIENT_REFERENCE_DB_POOL_MAX=1,INGREDIENT_SIGNAL_DB_POOL_MAX=1" \
  --command npm --args "run,relgraph:sync-routine:cron"

echo "== job: reviews-invitation-send (was a 60s bash loop)"
# Run the one-shot processor DIRECTLY, not scripts/run_reviews_invitation_send_loop.sh.
# That wrapper is `while true; do ...; sleep $SLEEP_SECONDS; done` and reads no RUN_ONCE (grep it) -
# it never exits. As a Cloud Run Job on a * * * * * trigger it would run to the 3600s task timeout
# while Scheduler starts another every minute, and Jobs allow concurrent executions. Each holds an
# asyncpg pool (db/database.py defaults 5..20), so within ~4-14 minutes of un-pausing at cutover the
# connection headroom is gone and every service starts throwing TooManyConnectionsError.
# The wrapper's own loop body is exactly this script, so a per-minute Scheduler tick IS the loop.
# DB_POOL_* are pinned because a Job inherits no pool sizing from the deploy scripts.
mkjob reviews-invitation-send "$BACKEND_IMAGE" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=reviews-invitation-send,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=4" \
  --task-timeout 300s \
  --command python --args "scripts/process_due_reviews_invitation_send_jobs.py"

# ---------------------------------------------------------------- 3. Cloud Scheduler triggers
PROJECT_NUMBER=$("$GCLOUD" projects describe "$PROJECT" --format='value(projectNumber)')
RUN_INVOKER="$SA"
have "$GCLOUD" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUN_INVOKER" --role=roles/run.invoker --condition=None
sched(){ # name schedule job-name
  local name="$1" cron="$2" job="$3"
  local uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$job:run"
  local verb=create; have "$GCLOUD" scheduler jobs describe "$name" --location "$REGION" && verb=update
  "$GCLOUD" scheduler jobs "$verb" http "$name" --location "$REGION" \
    --schedule="$cron" --time-zone=Etc/UTC --uri="$uri" --http-method=POST \
    --oauth-service-account-email="$RUN_INVOKER" \
    --attempt-deadline=1800s --quiet
  # FAIL CLOSED on anything that is not exactly 0. `[ "$PAUSED" = 1 ]` treated PAUSED=true, yes, on
  # and "1 " as UNPAUSED - an operator typing PAUSED=true for safety would have armed
  # reviews-invitation-send on a one-minute schedule against a copy of production.
  case "$PAUSED" in
    0) "$GCLOUD" scheduler jobs resume "$name" --location "$REGION" --quiet \
         || { echo "FAILED to resume $name" >&2; exit 1; }
       echo "   (RESUMED: $name)" ;;
    *) "$GCLOUD" scheduler jobs pause "$name" --location "$REGION" --quiet \
         || { echo "FAILED to pause $name - it may be LIVE" >&2; exit 1; }
       echo "   (paused: $name)" ;;
  esac
}
echo "== scheduler triggers"
sched relgraph-sync-cron "37 10 * * *" relgraph-sync
sched reviews-invitation-send-cron "* * * * *" reviews-invitation-send

echo
echo "worker:    $("$GCLOUD" run services describe worker --region "$REGION" --format='value(status.url)') (min=max=1, AUDIT_WORKER_ENABLED=$WORKERS)"
"$GCLOUD" scheduler jobs list --location "$REGION" --format="table(name.basename(),schedule,state)"
