#!/usr/bin/env bash
# Provision only the Store Audit commerce lane.  This deliberately does not
# touch the shared worker, unrelated Cloud Run Jobs, or their schedulers.
set -euo pipefail

ENV="${1:-}"; BACKEND_TAG="${2:-}"; BROWSER_TAG="${3:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <backend-tag> <browser-tag>" >&2; exit 2 ;;
esac
[ -n "$BACKEND_TAG" ] && [ -n "$BROWSER_TAG" ] || { echo "backend-tag and browser-tag are required" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"; REGION=us-west1; SHARED=pivota-shared
# shellcheck source=infra/gcp/_serving_revision.sh
. "$(cd "$(dirname "$0")" && pwd)/_serving_revision.sh"
SECRET=STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY
CRAWL_SA="sa-store-audit-commerce-crawl@$PROJECT.iam.gserviceaccount.com"
SELECTOR_SA="sa-store-audit-commerce-sel@$PROJECT.iam.gserviceaccount.com"
SCHEDULER_SA="sa-store-audit-commerce-sched@$PROJECT.iam.gserviceaccount.com"
BACKEND_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/backend:$BACKEND_TAG"
BROWSER_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/store-audit-browser:$BROWSER_TAG"
have(){ "$@" >/dev/null 2>&1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"

for account in "$CRAWL_SA" "$SELECTOR_SA" "$SCHEDULER_SA"; do
  have "$GCLOUD" iam service-accounts describe "$account" \
    || { echo "missing $account; run setup_store_audit_commerce_identity.sh first" >&2; exit 1; }
done

# Jobs must always target the canonical web origin, never a tagged revision or
# an operator-supplied host.  The active revision must already be receipt-ready
# before a job with the receipt key can exist.
WEB_SPEC="$("$GCLOUD" run services describe web --region "$REGION" --format=json)"
WEB_URL="$(printf '%s' "$WEB_SPEC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["url"])')"
# THE 100%-TRAFFIC REVISION, via the shared helper. The copy that used to live here also
# required `not x.get("tag")` — and deploy_backend.sh tags EVERY candidate (`--tag c-<sha>`)
# and the promoted revision keeps that tag until a later sweep, so on real production that
# filter matched nothing and this script exited 2 ("web needs exactly one untagged
# 100%-traffic revision") every time it was run. Verified against prod 2026-09-06:
# web-00560-caw serves 100% carrying tag c-d222cb8c4a51. setup_scheduler.sh removed the same
# conjunct and wrote down why; this sibling copy kept it, which is the drift a shared
# definition exists to stop.
WEB_REVISION="$(serving_revision web || true)"
[ -n "$WEB_REVISION" ] || { echo "web has no single 100%-traffic revision (traffic is split, or a rollout is half-finished) - resolve that before creating Store Audit commerce Jobs" >&2; exit 2; }
READY="$("$GCLOUD" run revisions describe "$WEB_REVISION" --region "$REGION" --format=json | python3 -c '
import json,sys
env=json.load(sys.stdin).get("spec",{}).get("containers",[{}])[0].get("env",[])
by={x.get("name"):x for x in env}
flag=by.get("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED",{}).get("value") == "true"
secret=by.get("STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY",{}).get("valueFrom",{}).get("secretKeyRef",{}).get("name") == "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY"
print("true" if flag and secret else "false")')"
[ "$READY" = true ] || { echo "active web revision must enable and mount $SECRET" >&2; exit 2; }

job(){ # name image service-account cpu memory subnet command args env secrets
  local name="$1" image="$2" account="$3" cpu="$4" memory="$5" subnet="$6" command="$7" args="$8" env="$9" secrets="${10}"
  local verb=create; have "$GCLOUD" run jobs describe "$name" --region "$REGION" && verb=update
  "$GCLOUD" run jobs "$verb" "$name" --region "$REGION" --image "$image" --service-account "$account" \
    --network default --subnet "$subnet" --vpc-egress all-traffic \
    --max-retries 1 --task-timeout 300s --cpu "$cpu" --memory "$memory" \
    --labels "env=$ENV,managed-by=infra-gcp,lane=store-audit-commerce" \
    --set-env-vars "$env" --set-secrets "$secrets" --command "$command" --args "$args" --quiet
}

echo "== Store Audit commerce selector (disarmed)"
job store-audit-commerce-reprobe-enqueue "$BACKEND_IMAGE" "$SELECTOR_SA" 1 2Gi default python \
  scripts/run_scheduled_commerce_checkout_reprobes.py \
  "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-commerce-reprobe-enqueue,PIVOTA_COMMIT_SHA=$BACKEND_TAG,STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED=true,STORE_AUDIT_COMMERCE_REPROBE_ARMED=false,STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED=true,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=3" \
  "DATABASE_URL=DATABASE_URL:latest,$SECRET=$SECRET:latest"

echo "== Store Audit commerce browser (disarmed, crawl subnet)"
job store-audit-commerce-probe "$BROWSER_IMAGE" "$CRAWL_SA" 2 2Gi pivota-crawl node \
  scripts/run_store_audit_commerce_worker.js \
  "PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=store-audit-commerce-probe,PIVOTA_COMMIT_SHA=$BROWSER_TAG,STORE_AUDIT_COMMERCE_REPROBE_ARMED=false,STORE_AUDIT_COMMERCE_PROBE_CLAIM_URL=$WEB_URL/internal/store-audit/commerce-probes/claims,STORE_AUDIT_COMMERCE_PROBE_RECEIPT_URL=$WEB_URL/internal/store-audit/commerce-probes/receipts,STORE_AUDIT_COMMERCE_PROBE_ID_TOKEN_AUDIENCE=$WEB_URL" \
  "$SECRET=$SECRET:latest"

"$GCLOUD" run services add-iam-policy-binding web --region "$REGION" --member="serviceAccount:$CRAWL_SA" --role=roles/run.invoker --quiet
for name in store-audit-commerce-reprobe-enqueue store-audit-commerce-probe; do
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
scheduler store-audit-commerce-reprobe-enqueue-cron "45 3 * * *" store-audit-commerce-reprobe-enqueue
scheduler store-audit-commerce-probe-cron "*/5 * * * *" store-audit-commerce-probe
echo "Store Audit commerce Jobs created disarmed; both Scheduler triggers are paused."
