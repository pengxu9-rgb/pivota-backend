#!/usr/bin/env bash
# Provision least-privilege identities for the anonymous Store Audit commerce
# browser lane. The key is pre-created and rotated separately; this script
# never creates or reveals a secret value.
set -euo pipefail

ENV="${1:-}"
case "$ENV" in
  staging) PROJECT=pivota-staging ;;
  prod) PROJECT=pivota-prod ;;
  *) echo "usage: $0 staging|prod" >&2; exit 2 ;;
esac

GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
SHARED=pivota-shared
CRAWL_SA="sa-store-audit-commerce-crawl@$PROJECT.iam.gserviceaccount.com"
SELECTOR_SA="sa-store-audit-commerce-selector@$PROJECT.iam.gserviceaccount.com"
SCHEDULER_SA="sa-store-audit-commerce-scheduler@$PROJECT.iam.gserviceaccount.com"
BACKEND_SA="sa-backend@$PROJECT.iam.gserviceaccount.com"
SECRET=STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY
have(){ "$@" >/dev/null 2>&1; }
retry(){ local n=0; until "$@"; do n=$((n+1)); [ "$n" -ge 8 ] && return 1; sleep $((n*5)); done; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"

have "$GCLOUD" secrets describe "$SECRET" || { echo "missing dedicated secret $SECRET" >&2; exit 1; }
"$GCLOUD" secrets versions access latest --secret="$SECRET" | grep -q . \
  || { echo "dedicated secret $SECRET has no non-empty latest version" >&2; exit 1; }

ensure_sa(){
  local short="$1" email="$2" purpose="$3"
  have "$GCLOUD" iam service-accounts describe "$email" \
    || "$GCLOUD" iam service-accounts create "$short" --display-name="$purpose" --quiet
}
grant_project(){
  retry "$GCLOUD" projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$1" --role="$2" --condition=None --quiet >/dev/null
}
grant_secret(){
  retry "$GCLOUD" secrets add-iam-policy-binding "$2" \
    --member="serviceAccount:$1" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
}
grant_registry(){
  retry "$GCLOUD" artifacts repositories add-iam-policy-binding pivota \
    --location="$REGION" --project="$SHARED" --member="serviceAccount:$1" \
    --role=roles/artifactregistry.reader --quiet >/dev/null
}

ensure_sa sa-store-audit-commerce-crawl "$CRAWL_SA" "Store Audit anonymous commerce browser"
ensure_sa sa-store-audit-commerce-selector "$SELECTOR_SA" "Store Audit commerce route selector"
ensure_sa sa-store-audit-commerce-scheduler "$SCHEDULER_SA" "Store Audit commerce Scheduler invoker"
for account in "$CRAWL_SA" "$SELECTOR_SA"; do
  grant_project "$account" roles/logging.logWriter
  grant_project "$account" roles/monitoring.metricWriter
  grant_project "$account" roles/cloudtrace.agent
  grant_registry "$account"
done
grant_secret "$CRAWL_SA" "$SECRET"
grant_secret "$SELECTOR_SA" "$SECRET"
# The receipt endpoint is served by `web`, which runs as sa-backend.  Grant
# this key explicitly rather than relying on a broad project-level Secret
# Manager role; otherwise Cloud Run cannot mount the declared secret.
grant_secret "$BACKEND_SA" "$SECRET"
grant_secret "$SELECTOR_SA" DATABASE_URL

echo "Store Audit commerce identities ready in $PROJECT"
echo "  backend:   $BACKEND_SA (only $SECRET)"
echo "  crawl:     $CRAWL_SA (only $SECRET)"
echo "  selector:  $SELECTOR_SA (only DATABASE_URL + $SECRET)"
echo "  scheduler: $SCHEDULER_SA (no secrets; job-specific Run invoke is granted by setup_scheduler.sh)"
