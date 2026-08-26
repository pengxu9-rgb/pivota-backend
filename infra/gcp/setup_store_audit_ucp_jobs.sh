#!/usr/bin/env bash
# Provision only the Store Audit UCP lane's two Cloud Run Jobs and their paused
# Scheduler triggers.  This deliberately does not touch the shared worker,
# unrelated Cloud Run Jobs, or their schedulers — the same isolation the
# commerce lane got in setup_store_audit_commerce_jobs.sh, and for the same
# reason: setup_scheduler.sh re-provisions EVERY job at the tags it is handed.
set -euo pipefail

ENV="${1:-}"; BACKEND_TAG="${2:-}"; GATEWAY_TAG="${3:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <backend-tag> <gateway-tag>" >&2; exit 2 ;;
esac
[ -n "$BACKEND_TAG" ] && [ -n "$GATEWAY_TAG" ] || { echo "backend-tag and gateway-tag are required" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; SHARED=pivota-shared
SECRET=STORE_AUDIT_UCP_PROBE_INTERNAL_KEY
CRAWL_SA="sa-store-audit-ucp-crawl@$PROJECT.iam.gserviceaccount.com"
SELECTOR_SA="sa-store-audit-ucp-selector@$PROJECT.iam.gserviceaccount.com"
SCHEDULER_SA="sa-store-audit-ucp-scheduler@$PROJECT.iam.gserviceaccount.com"
BACKEND_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/backend:$BACKEND_TAG"
GATEWAY_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/gateway:$GATEWAY_TAG"
have(){ "$@" >/dev/null 2>&1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"

for account in "$CRAWL_SA" "$SELECTOR_SA" "$SCHEDULER_SA"; do
  have "$GCLOUD" iam service-accounts describe "$account" \
    || { echo "missing $account; run setup_store_audit_ucp_identity.sh first" >&2; exit 1; }
done
for image in "$BACKEND_IMAGE" "$GATEWAY_IMAGE"; do
  have "$GCLOUD" artifacts docker images describe "$image" \
    || { echo "missing image $image; build it before provisioning jobs" >&2; exit 1; }
done

# Jobs must always target the canonical web origin, never a tagged revision or
# an operator-supplied host.  The active revision must already be receipt-ready
# before a job with the receipt key can exist.
WEB_SPEC="$("$GCLOUD" run services describe web --region "$REGION" --format=json)"
WEB_URL="$(printf '%s' "$WEB_SPEC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["url"])')"
# The serving revision may appear either as a pinned revisionName or as a
# latestRevision=true target (deploy_backend.sh promotion leaves the candidate
# tag riding on the latest entry), so resolve both shapes.
WEB_REVISION="$(printf '%s' "$WEB_SPEC" | python3 -c '
import json,sys
status=json.load(sys.stdin).get("status",{})
full=[x for x in status.get("traffic",[]) if x.get("percent")==100]
if len(full)!=1: print(""); raise SystemExit
entry=full[0]
print(status.get("latestReadyRevisionName","") if entry.get("latestRevision") else entry.get("revisionName",""))')"
[ -n "$WEB_REVISION" ] || { echo "web needs exactly one 100%-traffic revision" >&2; exit 2; }
READY="$("$GCLOUD" run revisions describe "$WEB_REVISION" --region "$REGION" --format=json | python3 -c '
import json,sys
env=json.load(sys.stdin).get("spec",{}).get("containers",[{}])[0].get("env",[])
by={x.get("name"):x for x in env}
flag=by.get("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED",{}).get("value") == "true"
secret=by.get("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY",{}).get("valueFrom",{}).get("secretKeyRef",{}).get("name") == "STORE_AUDIT_UCP_PROBE_INTERNAL_KEY"
print("true" if flag and secret else "false")')"
[ "$READY" = true ] || { echo "active web revision ($WEB_REVISION) must enable and mount $SECRET" >&2; exit 2; }

job(){ # name image service-account cpu memory subnet command args env secrets
  local name="$1" image="$2" account="$3" cpu="$4" memory="$5" subnet="$6" command="$7" args="$8" env="$9" secrets="${10}"
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$account" \
    --network default --subnet "$subnet" --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 300s --cpu "$cpu" --memory "$memory" \
    --labels "env=$ENV,managed-by=infra-gcp,lane=store-audit-ucp" \
    --set-env-vars "$env" --set-secrets "$secrets" --command "$command" --args "$args" --quiet
}

echo "== Store Audit UCP selector (domain/TTL reprobe enqueue)"
job store-audit-ucp-reprobe-enqueue "$BACKEND_IMAGE" "$SELECTOR_SA" 1 2Gi default python \
  scripts/run_scheduled_ucp_reprobes.py \
  "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-ucp-reprobe-enqueue,PIVOTA_COMMIT_SHA=$BACKEND_TAG,STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED=true,STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED=true,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
  "DATABASE_URL=DATABASE_URL:latest,$SECRET=$SECRET:latest"

echo "== Store Audit UCP probe (anonymous protocol crawl, crawl subnet)"
job store-audit-ucp-probe "$GATEWAY_IMAGE" "$CRAWL_SA" 1 1Gi pivota-crawl node \
  scripts/run_store_audit_ucp_worker.js \
  "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-ucp-probe,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,STORE_AUDIT_UCP_PROBE_CLAIM_URL=$WEB_URL/internal/store-audit/ucp-probes/claims,STORE_AUDIT_UCP_PROBE_RECEIPT_URL=$WEB_URL/internal/store-audit/ucp-probes/receipts,STORE_AUDIT_UCP_PROBE_ID_TOKEN_AUDIENCE=$WEB_URL" \
  "$SECRET=$SECRET:latest"

"$GCLOUD" run services add-iam-policy-binding web --region "$REGION" --member="serviceAccount:$CRAWL_SA" --role=roles/run.invoker --quiet
for name in store-audit-ucp-reprobe-enqueue store-audit-ucp-probe; do
  "$GCLOUD" run jobs add-iam-policy-binding "$name" --region "$REGION" --member="serviceAccount:$SCHEDULER_SA" --role=roles/run.invoker --quiet
done

scheduler(){ # name schedule job
  local name="$1" schedule="$2" target="$3" verb=create
  have "$GCLOUD" scheduler jobs describe "$name" --location "$REGION" && verb=update
  "$GCLOUD" scheduler jobs "$verb" http "$name" --location "$REGION" --schedule="$schedule" --time-zone=Etc/UTC \
    --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$target:run" --http-method=POST \
    --oauth-service-account-email="$SCHEDULER_SA" --attempt-deadline=300s --quiet
  "$GCLOUD" scheduler jobs pause "$name" --location "$REGION" --quiet
}
scheduler store-audit-ucp-reprobe-enqueue-cron "30 3 * * *" store-audit-ucp-reprobe-enqueue
scheduler store-audit-ucp-probe-cron "*/5 * * * *" store-audit-ucp-probe
echo "Store Audit UCP Jobs created; both Scheduler triggers are paused."
echo "Arming is an explicit resume:"
echo "  gcloud scheduler jobs resume store-audit-ucp-probe-cron --location $REGION --project $PROJECT"
echo "  gcloud scheduler jobs resume store-audit-ucp-reprobe-enqueue-cron --location $REGION --project $PROJECT"
