#!/usr/bin/env bash
# Deploy the backend image to Cloud Run in one environment.
#   infra/gcp/deploy_backend.sh staging|prod <image-tag>   (tag = git sha pushed by cloudbuild.backend.yaml)
# Prereqs: bootstrap_env.sh ran; port_railway_env.py --apply ran (env.<env>.yaml + secrets.<env>.list exist).
set -euo pipefail
ENV="${1:-}"; TAG="${2:-}"
[ -n "$ENV" ] && [ -n "$TAG" ] || { echo "usage: $0 staging|prod <image-tag>" >&2; exit 2; }
case "$ENV" in
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging;    MIN=1; MAX=4;  CPU=2; MEM=2Gi ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production; MIN=2; MAX=20; CPU=2; MEM=4Gi ;;
  *) echo "bad env" >&2; exit 2 ;;
esac
HERE="$(cd "$(dirname "$0")" && pwd)"
GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
SERVICE="${SERVICE:-web}"
IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/backend:$TAG"
ENV_FILE="$HERE/env.$ENV.yaml"
SECRETS_FILE="$HERE/secrets.$ENV.list"
[ -f "$ENV_FILE" ] && [ -f "$SECRETS_FILE" ] || { echo "missing $ENV_FILE / $SECRETS_FILE — run port_railway_env.py first" >&2; exit 1; }

# Secret Manager mappings: DATABASE_URL/REDIS_URL from bootstrap + every env-* secret
SECRETS="DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest,$(paste -sd, "$SECRETS_FILE")"

# gcloud allows only ONE env-vars flag: merge the ported file with the platform vars into a temp file
MERGED=$(mktemp); trap 'rm -f "$MERGED"' EXIT
{ cat "$ENV_FILE"; printf 'PIVOTA_ENV: "%s"\nPIVOTA_SERVICE_NAME: "%s"\nPIVOTA_COMMIT_SHA: "%s"\nPIVOTA_PLATFORM: "cloud_run"\n' "$PIVOTA_ENV" "$SERVICE" "$TAG"; } > "$MERGED"

"$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "sa-backend@$PROJECT.iam.gserviceaccount.com" \
  --network default --subnet default --vpc-egress private-ranges-only \
  --env-vars-file "$MERGED" \
  --set-secrets "$SECRETS" \
  --port 8080 --cpu "$CPU" --memory "$MEM" --concurrency 80 --timeout 300 \
  --min-instances "$MIN" --max-instances "$MAX" \
  --no-cpu-throttling --cpu-boost \
  --execution-environment gen2 \
  --allow-unauthenticated \
  --labels "env=$ENV,service=$SERVICE,managed-by=infra-gcp" \
  --quiet

URL=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
echo "deployed $SERVICE -> $URL"
echo "health:"; curl -sS -m 20 "$URL/health" | head -c 600; echo
