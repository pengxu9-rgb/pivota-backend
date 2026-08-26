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
: "${RELGRAPH_PUBLICATION_WORKER:=false}"
: "${SEARCH_INDEX_PUBLICATION_WORKER:=false}"
: "${CHECKOUT_VALIDATION_WORKER:=false}"
: "${INSIGHT_REFRESH_WORKER:=false}"
: "${STORE_AUDIT_UCP_REPROBE_WORKER:=false}"
: "${STORE_AUDIT_UCP_REPROBE_ARMED:=false}"
: "${STORE_AUDIT_COMMERCE_REPROBE_WORKER:=false}"
: "${STORE_AUDIT_COMMERCE_REPROBE_ARMED:=false}"
# The destination sweep OBSERVES by default and RETIRES only when armed. Observing is safe
# and useful on its own — the readiness gate blocks a never-verified seed, so the sweep has to
# run before the external lane can serve at all. Retiring writes status=inactive and suppresses
# the mirrored catalog rows, which is a different blast radius and gets its own switch.
: "${EXTERNAL_SEED_DESTINATION_SWEEP:=false}"
: "${EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE:=false}"
case "$WORKERS" in true|false) ;; *) echo "WORKERS must be exactly true or false (got '$WORKERS')" >&2; exit 2 ;; esac
case "$PAUSED"  in 0|1)         ;; *) echo "PAUSED must be exactly 0 or 1 (got '$PAUSED')" >&2; exit 2 ;; esac
case "$RELGRAPH_PUBLICATION_WORKER" in true|false) ;; *) echo "RELGRAPH_PUBLICATION_WORKER must be exactly true or false (got '$RELGRAPH_PUBLICATION_WORKER')" >&2; exit 2 ;; esac
case "$SEARCH_INDEX_PUBLICATION_WORKER" in true|false) ;; *) echo "SEARCH_INDEX_PUBLICATION_WORKER must be exactly true or false (got '$SEARCH_INDEX_PUBLICATION_WORKER')" >&2; exit 2 ;; esac
case "$CHECKOUT_VALIDATION_WORKER" in true|false) ;; *) echo "CHECKOUT_VALIDATION_WORKER must be exactly true or false (got '$CHECKOUT_VALIDATION_WORKER')" >&2; exit 2 ;; esac
case "$INSIGHT_REFRESH_WORKER" in true|false) ;; *) echo "INSIGHT_REFRESH_WORKER must be exactly true or false (got '$INSIGHT_REFRESH_WORKER')" >&2; exit 2 ;; esac
case "$STORE_AUDIT_UCP_REPROBE_WORKER" in true|false) ;; *) echo "STORE_AUDIT_UCP_REPROBE_WORKER must be exactly true or false (got '$STORE_AUDIT_UCP_REPROBE_WORKER')" >&2; exit 2 ;; esac
case "$STORE_AUDIT_UCP_REPROBE_ARMED" in true|false) ;; *) echo "STORE_AUDIT_UCP_REPROBE_ARMED must be exactly true or false (got '$STORE_AUDIT_UCP_REPROBE_ARMED')" >&2; exit 2 ;; esac
case "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" in true|false) ;; *) echo "STORE_AUDIT_COMMERCE_REPROBE_WORKER must be exactly true or false (got '$STORE_AUDIT_COMMERCE_REPROBE_WORKER')" >&2; exit 2 ;; esac
case "$STORE_AUDIT_COMMERCE_REPROBE_ARMED" in true|false) ;; *) echo "STORE_AUDIT_COMMERCE_REPROBE_ARMED must be exactly true or false (got '$STORE_AUDIT_COMMERCE_REPROBE_ARMED')" >&2; exit 2 ;; esac
case "$EXTERNAL_SEED_DESTINATION_SWEEP" in true|false) ;; *) echo "EXTERNAL_SEED_DESTINATION_SWEEP must be exactly true or false (got '$EXTERNAL_SEED_DESTINATION_SWEEP')" >&2; exit 2 ;; esac
case "$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE" in true|false) ;; *) echo "EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE must be exactly true or false (got '$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE')" >&2; exit 2 ;; esac
if [ "$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE" = true ] && [ "$EXTERNAL_SEED_DESTINATION_SWEEP" != true ]; then
  echo "EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE=true requires EXTERNAL_SEED_DESTINATION_SWEEP=true" >&2; exit 2
fi
if [ "$STORE_AUDIT_UCP_REPROBE_ARMED" = true ] && [ "$STORE_AUDIT_UCP_REPROBE_WORKER" != true ]; then
  echo "STORE_AUDIT_UCP_REPROBE_ARMED=true requires STORE_AUDIT_UCP_REPROBE_WORKER=true" >&2; exit 2
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_ARMED" = true ] && [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" != true ]; then
  echo "STORE_AUDIT_COMMERCE_REPROBE_ARMED=true requires STORE_AUDIT_COMMERCE_REPROBE_WORKER=true" >&2; exit 2
fi
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ] && [ "$PAUSED" = 0 ]; then
  echo "refusing PAUSED=0 with Store Audit UCP enabled: it resumes unrelated schedulers; use PAUSED=1 STORE_AUDIT_UCP_REPROBE_ARMED=true to arm only UCP" >&2
  exit 2
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ] && [ "$PAUSED" = 0 ]; then
  echo "refusing PAUSED=0 with Store Audit commerce enabled: use PAUSED=1 STORE_AUDIT_COMMERCE_REPROBE_ARMED=true to arm only commerce" >&2
  exit 2
fi
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ]; then
  # Validate before touching even the ordinary worker/jobs below. A missing
  # Store Audit endpoint must be a no-write configuration error, not a partial
  # scheduler reconciliation followed by a failure.
  : "${STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL:?set an https backend base URL before enabling Store Audit UCP jobs}"
  [[ "$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL" =~ ^https://[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
    || { echo "STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL must be a bare HTTPS origin (no path, query, userinfo, or port)" >&2; exit 2; }
  STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL="${STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL%/}"
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ]; then
  : "${STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL:?set an https backend base URL before enabling Store Audit commerce jobs}"
  [[ "$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL" =~ ^https://[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
    || { echo "STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL must be a bare HTTPS origin (no path, query, userinfo, or port)" >&2; exit 2; }
  STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL="${STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL%/}"
fi
GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; HERE="$(cd "$(dirname "$0")" && pwd)"
export CLOUDSDK_CORE_PROJECT="$PROJECT"
BACKEND_IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/backend:$BACKEND_TAG"
GATEWAY_IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/gateway:$GATEWAY_TAG"
BROWSER_AUDIT_IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/store-audit-browser:$GATEWAY_TAG"
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
UCP_CRAWL_SA="sa-store-audit-ucp-crawl@$PROJECT.iam.gserviceaccount.com"
UCP_SELECTOR_SA="sa-store-audit-ucp-selector@$PROJECT.iam.gserviceaccount.com"
UCP_SCHEDULER_SA="sa-store-audit-ucp-scheduler@$PROJECT.iam.gserviceaccount.com"
COMMERCE_CRAWL_SA="sa-store-audit-commerce-crawl@$PROJECT.iam.gserviceaccount.com"
COMMERCE_SELECTOR_SA="sa-store-audit-commerce-sel@$PROJECT.iam.gserviceaccount.com"
COMMERCE_SCHEDULER_SA="sa-store-audit-commerce-sched@$PROJECT.iam.gserviceaccount.com"
have(){ "$@" >/dev/null 2>&1; }
require_service_account(){
  have "$GCLOUD" iam service-accounts describe "$1" \
    || { echo "missing $1; provision the matching Store Audit identity script for $ENV first" >&2; exit 1; }
}
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ]; then
  WEB_UCP_SPEC="$("$GCLOUD" run services describe web --region "$REGION" --format=json)"
  EXPECTED_UCP_BACKEND_BASE_URL="$(printf '%s' "$WEB_UCP_SPEC" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",{}).get("url", ""))')"
  [ "$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL" = "$EXPECTED_UCP_BACKEND_BASE_URL" ] \
    || { echo "STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL must exactly match web service URL ($EXPECTED_UCP_BACKEND_BASE_URL)" >&2; exit 2; }
  # The service template can describe a 0%-traffic candidate. The untagged
  # service URL used by the Job instead reaches the single untagged 100%
  # revision. Refuse a split/candidate state rather than declaring receipt
  # ready based on code that is not actually serving the URL.
  WEB_ACTIVE_REVISION="$(printf '%s' "$WEB_UCP_SPEC" | python3 -c '
import json,sys
o=json.load(sys.stdin)
active=[t.get("revisionName", "") for t in o.get("status",{}).get("traffic", []) if not t.get("tag") and t.get("percent") == 100]
print(active[0] if len(active) == 1 else "")
')"
  [ -n "$WEB_ACTIVE_REVISION" ] \
    || { echo "web must have exactly one untagged 100%-traffic revision before Store Audit Jobs are created" >&2; exit 2; }
  WEB_ACTIVE_SPEC="$("$GCLOUD" run revisions describe "$WEB_ACTIVE_REVISION" --region "$REGION" --format=json)"
  # Refuse to create or arm a crawler against a revision where Cloud Run would
  # accept OIDC but the app would still return a disabled-receipt 404. This
  # reads only the active revision's configuration; it never renders a secret.
  WEB_UCP_RECEIPT_READY="$(printf '%s' "$WEB_ACTIVE_SPEC" | python3 -c '
import json,sys
o=json.load(sys.stdin)
env=o.get("spec",{}).get("containers",[{}])[0].get("env",[])
by_name={e.get("name"):e for e in env}
flag=by_name.get("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED",{}).get("value") == "true"
secret=by_name.get("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY",{}).get("valueFrom",{}).get("secretKeyRef",{}).get("name") == "STORE_AUDIT_UCP_PROBE_INTERNAL_KEY"
print("true" if flag and secret else "false")
')"
  [ "$WEB_UCP_RECEIPT_READY" = true ] \
    || { echo "web must mount STORE_AUDIT_UCP_PROBE_INTERNAL_KEY and set STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED=true before Store Audit Jobs are created" >&2; exit 2; }
  require_service_account "$UCP_CRAWL_SA"
  require_service_account "$UCP_SELECTOR_SA"
  require_service_account "$UCP_SCHEDULER_SA"
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ]; then
  WEB_COMMERCE_SPEC="$("$GCLOUD" run services describe web --region "$REGION" --format=json)"
  EXPECTED_COMMERCE_BACKEND_BASE_URL="$(printf '%s' "$WEB_COMMERCE_SPEC" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",{}).get("url", ""))')"
  [ "$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL" = "$EXPECTED_COMMERCE_BACKEND_BASE_URL" ] \
    || { echo "STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL must exactly match web service URL ($EXPECTED_COMMERCE_BACKEND_BASE_URL)" >&2; exit 2; }
  WEB_COMMERCE_ACTIVE_REVISION="$(printf '%s' "$WEB_COMMERCE_SPEC" | python3 -c '
import json,sys
o=json.load(sys.stdin)
active=[t.get("revisionName", "") for t in o.get("status",{}).get("traffic", []) if not t.get("tag") and t.get("percent") == 100]
print(active[0] if len(active) == 1 else "")
')"
  [ -n "$WEB_COMMERCE_ACTIVE_REVISION" ] \
    || { echo "web must have exactly one untagged 100%-traffic revision before Store Audit commerce Jobs are created" >&2; exit 2; }
  WEB_COMMERCE_ACTIVE_SPEC="$("$GCLOUD" run revisions describe "$WEB_COMMERCE_ACTIVE_REVISION" --region "$REGION" --format=json)"
  WEB_COMMERCE_RECEIPT_READY="$(printf '%s' "$WEB_COMMERCE_ACTIVE_SPEC" | python3 -c '
import json,sys
o=json.load(sys.stdin)
env=o.get("spec",{}).get("containers",[{}])[0].get("env",[])
by_name={e.get("name"):e for e in env}
flag=by_name.get("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED",{}).get("value") == "true"
secret=by_name.get("STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY",{}).get("valueFrom",{}).get("secretKeyRef",{}).get("name") == "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY"
print("true" if flag and secret else "false")
')"
  [ "$WEB_COMMERCE_RECEIPT_READY" = true ] \
    || { echo "web must mount STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY and set STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED=true before Store Audit commerce Jobs are created" >&2; exit 2; }
  require_service_account "$COMMERCE_CRAWL_SA"
  require_service_account "$COMMERCE_SELECTOR_SA"
  require_service_account "$COMMERCE_SCHEDULER_SA"
fi

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
mkjob(){ # name image service-account command...
  local name="$1" image="$2" service_account="$3"; shift 3
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$service_account" \
    --network default --subnet default --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 3600s --cpu 1 --memory 2Gi \
    --labels "env=$ENV,managed-by=infra-gcp" --quiet "$@"
}
mkcrawljob(){ # name image service-account command... -- the only crawl egress workload
  local name="$1" image="$2" service_account="$3"; shift 3
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$service_account" \
    --network default --subnet pivota-crawl --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 300s --cpu 1 --memory 1Gi \
    --labels "env=$ENV,managed-by=infra-gcp,lane=store-audit-crawl" --quiet "$@"
}
mkbrowserauditjob(){ # Browser is intentionally isolated to the crawl subnet.
  local name="$1" image="$2" service_account="$3"; shift 3
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$service_account" \
    --network default --subnet pivota-crawl --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 300s --cpu 2 --memory 2Gi \
    --labels "env=$ENV,managed-by=infra-gcp,lane=store-audit-crawl" --quiet "$@"
}
echo "== job: relgraph-sync (Railway cron 37 10 * * *)"
mkjob relgraph-sync "$GATEWAY_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=relgraph-sync,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,DB_POOL_MAX=3,PCI_KB_DB_POOL_MAX=1,INGREDIENT_REFERENCE_DB_POOL_MAX=1,INGREDIENT_SIGNAL_DB_POOL_MAX=1" \
  --command npm --args "run,relgraph:sync-routine:cron"

echo "== job: commerce-index-relgraph (v2 targeted graph publication)"
# This worker is independently opt-in. It is safe to create while inert, but it
# must not consume queue rows until migration 194 is applied, v2 fact emission is
# enabled, and the affected-product bridge has passed a canary. The script itself
# refuses to claim work without COMMERCE_INDEX_RELGRAPH_APPLY=true.
mkjob commerce-index-relgraph "$GATEWAY_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=commerce-index-relgraph,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,COMMERCE_INDEX_RELGRAPH_APPLY=$RELGRAPH_PUBLICATION_WORKER,DB_POOL_MAX=3,PCI_KB_DB_POOL_MAX=1" \
  --task-timeout 1800s \
  --command node --args "scripts/drain-commerce-index-relgraph.js,--worker-id,cloud-run-relgraph"

echo "== job: commerce-index-search-index (v2 targeted OpenSearch publication)"
# Requires CATALOG_SERVING_INDEX_BASE_URL and CATALOG_SERVING_INDEX_API_KEY to
# be provisioned on the job before SEARCH_INDEX_PUBLICATION_WORKER is enabled.
# The worker refuses to claim jobs if either the explicit apply gate or index
# configuration is absent, so an incomplete setup cannot drain the queue.
mkjob commerce-index-search-index "$GATEWAY_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest,CATALOG_SERVING_INDEX_API_KEY=CATALOG_SERVING_INDEX_API_KEY:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=commerce-index-search-index,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,COMMERCE_INDEX_SEARCH_PUBLICATION_APPLY=$SEARCH_INDEX_PUBLICATION_WORKER,DB_POOL_MAX=3,PCI_KB_DB_POOL_MAX=1" \
  --task-timeout 900s \
  --command node --args "scripts/drain-commerce-index-search-index.js,--worker-id,cloud-run-search-index"

echo "== job: commerce-index-checkout-validation (v2 quote-first marker)"
mkjob commerce-index-checkout-validation "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=commerce-index-checkout-validation,PIVOTA_COMMIT_SHA=$BACKEND_TAG,COMMERCE_INDEX_CHECKOUT_VALIDATION_ENABLED=$CHECKOUT_VALIDATION_WORKER,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
  --task-timeout 300s \
  --command python --args "scripts/drain_commerce_index_checkout_validation_jobs.py"

echo "== job: commerce-index-insight-refresh (v2 reviewed insights requests)"
mkjob commerce-index-insight-refresh "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=commerce-index-insight-refresh,PIVOTA_COMMIT_SHA=$BACKEND_TAG,COMMERCE_INDEX_INSIGHT_REFRESH_ENABLED=$INSIGHT_REFRESH_WORKER,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
  --task-timeout 300s \
  --command python --args "scripts/drain_commerce_index_insight_refresh_jobs.py"

echo "== job: reviews-invitation-send (was a 60s bash loop)"
# Run the one-shot processor DIRECTLY, not scripts/run_reviews_invitation_send_loop.sh.
# That wrapper is `while true; do ...; sleep $SLEEP_SECONDS; done` and reads no RUN_ONCE (grep it) -
# it never exits. As a Cloud Run Job on a * * * * * trigger it would run to the 3600s task timeout
# while Scheduler starts another every minute, and Jobs allow concurrent executions. Each holds an
# asyncpg pool (db/database.py defaults 5..20), so within ~4-14 minutes of un-pausing at cutover the
# connection headroom is gone and every service starts throwing TooManyConnectionsError.
# The wrapper's own loop body is exactly this script, so a per-minute Scheduler tick IS the loop.
# DB_POOL_* are pinned because a Job inherits no pool sizing from the deploy scripts.
mkjob reviews-invitation-send "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest,$(paste -sd, "$SECRETS_FILE")" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=reviews-invitation-send,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=4" \
  --task-timeout 300s \
  --command python --args "scripts/process_due_reviews_invitation_send_jobs.py"

echo "== job: content-canonical-election (GH Actions cron 52 */6 * * *)"
# Migrated from .github/workflows/content-canonical-election.yml, deleted in the
# same commit. Not a preference: Cloud SQL `pivota-pg` has NO public IP
# (ipv4Enabled=false, private 10.25.0.2 only), so a GitHub-hosted runner cannot
# reach the database from anywhere on the internet. The workflow held a
# DATABASE_URL repo secret pointing at the Railway public proxy; Railway was
# decommissioned 2026-08-25 and every scheduled run since has died in
# `database.connect()` with ConnectionResetError. There is no secret value that
# fixes it — the lane has to run inside the VPC.
#
# NOT a `mkcrawljob`: it fetches ONE URL, our own agent.pivota.cc sitemap, which
# is public-internet egress out of the `default` subnet through pivota-nat. The
# crawl subnet's reserved IP is for merchant storefronts and this is not one.
#
# `--apply` unconditionally, matching the workflow it replaces: a scheduled
# election auto-applied there too (the dispatch-only checkbox was for manual
# runs, and manual runs are now `gcloud run jobs execute`). --seed-from-sitemap
# every time is also deliberate and NOT a seed-once flag — stored winners
# outrank incumbency, so it changes nothing for settled content_keys and only
# supplies the tiebreak for groups being elected for the first time.
#
# The workflow also set DB_POOL_ACQUIRE_TIMEOUT_SECONDS=45. Deliberately NOT
# carried over: it existed because a GitHub runner reached Postgres over the
# public proxy, where the TLS+auth handshake regularly exceeded the 5s default.
# In-VPC to a private IP that reason is gone, and keeping it would only delay
# the failure signal on a genuinely wedged pool.
mkjob content-canonical-election "$BACKEND_IMAGE" "$SA" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=content-canonical-election,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2" \
  --task-timeout 900s \
  --command python \
  --args "scripts/elect_content_canonicals.py,--apply,--seed-from-sitemap,https://agent.pivota.cc/sitemap-products.xml"

# Store Audit UCP work has a dedicated, explicitly armed pair of Jobs. The
# selector only writes bounded verification rows; the probe Job is the sole
# workload placed on the crawl subnet. Do not create either Job until its
# dedicated receipt secret and staging endpoint exist: an empty/dummy key would
# turn a configuration omission into a silently unauthenticated lane.
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ]; then
  STORE_AUDIT_UCP_PROBE_CLAIM_URL="$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL/internal/store-audit/ucp-probes/claims"
  STORE_AUDIT_UCP_PROBE_RECEIPT_URL="$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL/internal/store-audit/ucp-probes/receipts"
  echo "== job: store-audit-ucp-reprobe-enqueue (domain/TTL selector)"
  mkjob store-audit-ucp-reprobe-enqueue "$BACKEND_IMAGE" "$UCP_SELECTOR_SA" \
    --set-secrets "DATABASE_URL=DATABASE_URL:latest,STORE_AUDIT_UCP_PROBE_INTERNAL_KEY=STORE_AUDIT_UCP_PROBE_INTERNAL_KEY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-ucp-reprobe-enqueue,PIVOTA_COMMIT_SHA=$BACKEND_TAG,STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED=true,STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED=true,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
    --task-timeout 300s \
    --command python --args "scripts/run_scheduled_ucp_reprobes.py"
  echo "== job: store-audit-ucp-probe (anonymous crawl egress)"
  mkcrawljob store-audit-ucp-probe "$GATEWAY_IMAGE" "$UCP_CRAWL_SA" \
    --set-secrets "STORE_AUDIT_UCP_PROBE_INTERNAL_KEY=STORE_AUDIT_UCP_PROBE_INTERNAL_KEY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-ucp-probe,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,STORE_AUDIT_UCP_PROBE_CLAIM_URL=$STORE_AUDIT_UCP_PROBE_CLAIM_URL,STORE_AUDIT_UCP_PROBE_RECEIPT_URL=$STORE_AUDIT_UCP_PROBE_RECEIPT_URL,STORE_AUDIT_UCP_PROBE_ID_TOKEN_AUDIENCE=$STORE_AUDIT_UCP_PROBE_BACKEND_BASE_URL" \
    --command node --args "scripts/run_store_audit_ucp_worker.js"
  "$GCLOUD" run services add-iam-policy-binding web --region "$REGION" \
    --member="serviceAccount:$UCP_CRAWL_SA" --role=roles/run.invoker --quiet
else
  echo "== Store Audit UCP Jobs not created (STORE_AUDIT_UCP_REPROBE_WORKER=false)"
fi

# Commerce checkout audit is merchant-scoped, so the selector emits at most
# one active probe per merchant. The browser job may add one item to cart and
# reveal checkout routing only; its backend receipt contract rejects address,
# payment, and order-submission data.
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ]; then
  COMMERCE_CLAIM_URL="$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL/internal/store-audit/commerce-probes/claims"
  COMMERCE_RECEIPT_URL="$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL/internal/store-audit/commerce-probes/receipts"
  echo "== job: store-audit-commerce-reprobe-enqueue (merchant checkout selector)"
  mkjob store-audit-commerce-reprobe-enqueue "$BACKEND_IMAGE" "$COMMERCE_SELECTOR_SA" \
    --set-secrets "DATABASE_URL=DATABASE_URL:latest,STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY=STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-commerce-reprobe-enqueue,PIVOTA_COMMIT_SHA=$BACKEND_TAG,STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED=true,STORE_AUDIT_COMMERCE_REPROBE_ARMED=$STORE_AUDIT_COMMERCE_REPROBE_ARMED,STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED=true,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
    --task-timeout 300s \
    --command python --args "scripts/run_scheduled_commerce_checkout_reprobes.py"
  echo "== job: store-audit-commerce-probe (anonymous browser crawl egress)"
  mkbrowserauditjob store-audit-commerce-probe "$BROWSER_AUDIT_IMAGE" "$COMMERCE_CRAWL_SA" \
    --set-secrets "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY=STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-commerce-probe,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,STORE_AUDIT_COMMERCE_REPROBE_ARMED=$STORE_AUDIT_COMMERCE_REPROBE_ARMED,STORE_AUDIT_COMMERCE_PROBE_CLAIM_URL=$COMMERCE_CLAIM_URL,STORE_AUDIT_COMMERCE_PROBE_RECEIPT_URL=$COMMERCE_RECEIPT_URL,STORE_AUDIT_COMMERCE_PROBE_ID_TOKEN_AUDIENCE=$STORE_AUDIT_COMMERCE_PROBE_BACKEND_BASE_URL" \
    --command node --args "scripts/run_store_audit_commerce_worker.js"
  "$GCLOUD" run services add-iam-policy-binding web --region "$REGION" \
    --member="serviceAccount:$COMMERCE_CRAWL_SA" --role=roles/run.invoker --quiet
else
  echo "== Store Audit commerce Jobs not created (STORE_AUDIT_COMMERCE_REPROBE_WORKER=false)"
fi

# ---------------------------------------------------------------- 3. Cloud Scheduler triggers
PROJECT_NUMBER=$("$GCLOUD" projects describe "$PROJECT" --format='value(projectNumber)')
RUN_INVOKER="$SA"
have "$GCLOUD" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUN_INVOKER" --role=roles/run.invoker --condition=None
sched(){ # name schedule job-name [invoker] [paused: 0|1]
  local name="$1" cron="$2" job="$3" invoker="${4:-$RUN_INVOKER}" paused="${5:-$PAUSED}"
  local uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$job:run"
  local verb=create; have "$GCLOUD" scheduler jobs describe "$name" --location "$REGION" && verb=update
  "$GCLOUD" scheduler jobs "$verb" http "$name" --location "$REGION" \
    --schedule="$cron" --time-zone=Etc/UTC --uri="$uri" --http-method=POST \
    --oauth-service-account-email="$invoker" \
    --attempt-deadline=1800s --quiet
  # FAIL CLOSED on anything that is not exactly 0. `[ "$PAUSED" = 1 ]` treated PAUSED=true, yes, on
  # and "1 " as UNPAUSED - an operator typing PAUSED=true for safety would have armed
  # reviews-invitation-send on a one-minute schedule against a copy of production.
  case "$paused" in
    0) "$GCLOUD" scheduler jobs resume "$name" --location "$REGION" --quiet \
         || { echo "FAILED to resume $name" >&2; exit 1; }
       echo "   (RESUMED: $name)" ;;
    *) "$GCLOUD" scheduler jobs pause "$name" --location "$REGION" --quiet \
         || { echo "FAILED to pause $name - it may be LIVE" >&2; exit 1; }
       echo "   (paused: $name)" ;;
  esac
}
# ---- external-seed destination sweep -------------------------------------------------------
# Re-reads the third-party product URLs we publish and withdraws the ones that are gone.
# `mkcrawljob`, not `mkjob`: it fetches merchant storefronts, so it must leave from the reserved
# pivota-crawl egress IP. A run from anywhere else is answered with a Cloudflare bot challenge by
# most brand hosts and reports "0 dead links" while having seen almost nothing.
# task-timeout is raised over the crawl default because per-host pacing is the point: ~1,700 seeds
# a day is a full corpus pass inside the 7-day staleness window.
if [ "$EXTERNAL_SEED_DESTINATION_SWEEP" = true ]; then
  echo "== job: external-seed-destination-sweep (retire=$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE)"
  SWEEP_ARGS="-m,jobs.external_seed_destination_sweep,--limit,1700"
  [ "$EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE" = true ] || SWEEP_ARGS="$SWEEP_ARGS,--no-retire"
  mkcrawljob external-seed-destination-sweep "$BACKEND_IMAGE" "$SA" \
    --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest" \
    --set-env-vars "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=external-seed-destination-sweep,PIVOTA_COMMIT_SHA=$BACKEND_TAG,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
    --task-timeout 3600s \
    --command python --args "$SWEEP_ARGS"
else
  echo "== external-seed destination sweep not created (EXTERNAL_SEED_DESTINATION_SWEEP=false)"
fi

echo "== scheduler triggers"
sched relgraph-sync-cron "37 10 * * *" relgraph-sync
if [ "$EXTERNAL_SEED_DESTINATION_SWEEP" = true ]; then
  sched external-seed-destination-sweep-cron "20 2 * * *" external-seed-destination-sweep
else
  # An idempotent disarm, not merely a creation skip: turning the flag off must stop an
  # existing trigger from continuing to crawl.
  if have "$GCLOUD" scheduler jobs describe external-seed-destination-sweep-cron --location "$REGION"; then
    "$GCLOUD" scheduler jobs pause external-seed-destination-sweep-cron --location "$REGION" --quiet
    echo "   (paused: external-seed-destination-sweep-cron; EXTERNAL_SEED_DESTINATION_SWEEP=false)"
  fi
fi
sched reviews-invitation-send-cron "* * * * *" reviews-invitation-send
# 00:52/06:52/12:52/18:52 UTC. The exact cron the GH workflow carried, and the
# offset is load-bearing: it LEADS pivota-agent-ui's sitemap cron (17 1,7,13,19)
# by 25 minutes, so the sitemap regenerating next reads a fresh election rather
# than one 5h35m old. Moving this without moving that one silently inverts the
# order.
sched content-canonical-election-cron "52 */6 * * *" content-canonical-election
sched commerce-index-relgraph-cron "*/10 * * * *" commerce-index-relgraph
sched commerce-index-search-index-cron "*/5 * * * *" commerce-index-search-index
sched commerce-index-checkout-validation-cron "*/5 * * * *" commerce-index-checkout-validation
sched commerce-index-insight-refresh-cron "*/10 * * * *" commerce-index-insight-refresh
if [ "$STORE_AUDIT_UCP_REPROBE_WORKER" = true ]; then
  for job in store-audit-ucp-reprobe-enqueue store-audit-ucp-probe; do
    "$GCLOUD" run jobs add-iam-policy-binding "$job" --region "$REGION" \
      --member="serviceAccount:$UCP_SCHEDULER_SA" --role=roles/run.invoker --quiet
  done
  UCP_PAUSED=1
  [ "$STORE_AUDIT_UCP_REPROBE_ARMED" = true ] && UCP_PAUSED=0
  sched store-audit-ucp-reprobe-enqueue-cron "30 3 * * *" store-audit-ucp-reprobe-enqueue "$UCP_SCHEDULER_SA" "$UCP_PAUSED"
  sched store-audit-ucp-probe-cron "*/5 * * * *" store-audit-ucp-probe "$UCP_SCHEDULER_SA" "$UCP_PAUSED"
else
  # Default false is an idempotent operational disarm, not merely a creation
  # skip. Existing Jobs may remain for forensic inspection, but their triggers
  # must never keep crawling after an operator turns the feature off.
  for trigger in store-audit-ucp-reprobe-enqueue-cron store-audit-ucp-probe-cron; do
    if have "$GCLOUD" scheduler jobs describe "$trigger" --location "$REGION"; then
      "$GCLOUD" scheduler jobs pause "$trigger" --location "$REGION" --quiet
      echo "   (paused: $trigger; STORE_AUDIT_UCP_REPROBE_WORKER=false)"
    fi
  done
fi
if [ "$STORE_AUDIT_COMMERCE_REPROBE_WORKER" = true ]; then
  for job in store-audit-commerce-reprobe-enqueue store-audit-commerce-probe; do
    "$GCLOUD" run jobs add-iam-policy-binding "$job" --region "$REGION" \
      --member="serviceAccount:$COMMERCE_SCHEDULER_SA" --role=roles/run.invoker --quiet
  done
  COMMERCE_PAUSED=1
  [ "$STORE_AUDIT_COMMERCE_REPROBE_ARMED" = true ] && COMMERCE_PAUSED=0
  sched store-audit-commerce-reprobe-enqueue-cron "45 3 * * *" store-audit-commerce-reprobe-enqueue "$COMMERCE_SCHEDULER_SA" "$COMMERCE_PAUSED"
  sched store-audit-commerce-probe-cron "*/5 * * * *" store-audit-commerce-probe "$COMMERCE_SCHEDULER_SA" "$COMMERCE_PAUSED"
else
  for trigger in store-audit-commerce-reprobe-enqueue-cron store-audit-commerce-probe-cron; do
    if have "$GCLOUD" scheduler jobs describe "$trigger" --location "$REGION"; then
      "$GCLOUD" scheduler jobs pause "$trigger" --location "$REGION" --quiet
      echo "   (paused: $trigger; STORE_AUDIT_COMMERCE_REPROBE_WORKER=false)"
    fi
  done
fi
# A global scheduler resume must never activate this new writer by accident.
if [ "$RELGRAPH_PUBLICATION_WORKER" != true ]; then
  "$GCLOUD" scheduler jobs pause commerce-index-relgraph-cron --location "$REGION" --quiet
  echo "   (paused: commerce-index-relgraph-cron; RELGRAPH_PUBLICATION_WORKER=false)"
fi
if [ "$SEARCH_INDEX_PUBLICATION_WORKER" != true ]; then
  "$GCLOUD" scheduler jobs pause commerce-index-search-index-cron --location "$REGION" --quiet
  echo "   (paused: commerce-index-search-index-cron; SEARCH_INDEX_PUBLICATION_WORKER=false)"
fi
if [ "$CHECKOUT_VALIDATION_WORKER" != true ]; then
  "$GCLOUD" scheduler jobs pause commerce-index-checkout-validation-cron --location "$REGION" --quiet
  echo "   (paused: commerce-index-checkout-validation-cron; CHECKOUT_VALIDATION_WORKER=false)"
fi
if [ "$INSIGHT_REFRESH_WORKER" != true ]; then
  "$GCLOUD" scheduler jobs pause commerce-index-insight-refresh-cron --location "$REGION" --quiet
  echo "   (paused: commerce-index-insight-refresh-cron; INSIGHT_REFRESH_WORKER=false)"
fi

echo
echo "worker:    $("$GCLOUD" run services describe worker --region "$REGION" --format='value(status.url)') (min=max=1, AUDIT_WORKER_ENABLED=$WORKERS)"
"$GCLOUD" scheduler jobs list --location "$REGION" --format="table(name.basename(),schedule,state)"
