#!/usr/bin/env bash
# Provision the three least-privilege identities for the Store Audit UCP lane.
#
#   infra/gcp/setup_store_audit_ucp_identity.sh staging|prod
#
# This is deliberately separate from bootstrap_env.sh. Existing worker accounts
# have broad historical permissions; granting the probe one more role would not
# make the anonymous crawler a bounded trust domain.
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
CRAWL_SA="sa-store-audit-ucp-crawl@$PROJECT.iam.gserviceaccount.com"
SELECTOR_SA="sa-store-audit-ucp-selector@$PROJECT.iam.gserviceaccount.com"
SCHEDULER_SA="sa-store-audit-ucp-scheduler@$PROJECT.iam.gserviceaccount.com"
SECRET=STORE_AUDIT_UCP_PROBE_INTERNAL_KEY
have(){ "$@" >/dev/null 2>&1; }
retry(){ local n=0; until "$@"; do n=$((n+1)); [ "$n" -ge 8 ] && return 1; sleep $((n*5)); done; }

export CLOUDSDK_CORE_PROJECT="$PROJECT"

# The key must already have a non-empty version. Secret creation/value rotation
# is an explicit security ceremony, never a side effect of job provisioning.
have "$GCLOUD" secrets describe "$SECRET" || { echo "missing dedicated secret $SECRET" >&2; exit 1; }
"$GCLOUD" secrets versions access latest --secret="$SECRET" | grep -q . \
  || { echo "dedicated secret $SECRET has no non-empty latest version" >&2; exit 1; }

ensure_sa(){
  local short="$1" email="$2" purpose="$3"
  have "$GCLOUD" iam service-accounts describe "$email" \
    || "$GCLOUD" iam service-accounts create "$short" --display-name="$purpose" --quiet
}
grant_project(){
  local email="$1" role="$2"
  retry "$GCLOUD" projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$email" --role="$role" --condition=None --quiet >/dev/null
}
grant_secret(){
  local email="$1" secret="$2"
  retry "$GCLOUD" secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:$email" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
}
grant_registry(){
  local email="$1"
  retry "$GCLOUD" artifacts repositories add-iam-policy-binding pivota \
    --location="$REGION" --project="$SHARED" --member="serviceAccount:$email" \
    --role=roles/artifactregistry.reader --quiet >/dev/null
}

ensure_sa sa-store-audit-ucp-crawl "$CRAWL_SA" "Store Audit UCP anonymous crawl"
ensure_sa sa-store-audit-ucp-selector "$SELECTOR_SA" "Store Audit UCP route selector"
ensure_sa sa-store-audit-ucp-scheduler "$SCHEDULER_SA" "Store Audit UCP Scheduler invoker"

# Crawl has exactly one secret and cannot reach Cloud SQL because it receives no
# database credential. Selector has only the database URL and receipt key it
# needs to enqueue safely. Scheduler has no Secret Manager permission.
for account in "$CRAWL_SA" "$SELECTOR_SA"; do
  grant_project "$account" roles/logging.logWriter
  grant_project "$account" roles/monitoring.metricWriter
  grant_project "$account" roles/cloudtrace.agent
  grant_registry "$account"
done
grant_secret "$CRAWL_SA" "$SECRET"
grant_secret "$SELECTOR_SA" "$SECRET"
grant_secret "$SELECTOR_SA" DATABASE_URL

echo "Store Audit UCP identities ready in $PROJECT"
echo "  crawl:     $CRAWL_SA (only $SECRET)"
echo "  selector:  $SELECTOR_SA (only DATABASE_URL + $SECRET)"
echo "  scheduler: $SCHEDULER_SA (no secrets; job-specific Run invoke is granted by setup_scheduler.sh)"
