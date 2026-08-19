#!/usr/bin/env bash
# Deploy the backend image to Cloud Run in one environment.
#   infra/gcp/deploy_backend.sh staging|prod <image-tag>   (tag = git sha pushed by cloudbuild.backend.yaml)
# Prereqs: bootstrap_env.sh ran; port_railway_env.py --apply ran (env.<env>.yaml + secrets.<env>.list exist).
set -euo pipefail
ENV="${1:-}"; TAG="${2:-}"
[ -n "$ENV" ] && [ -n "$TAG" ] || { echo "usage: $0 staging|prod <image-tag>" >&2; exit 2; }
case "$ENV" in
  # WORKERS: only the production service drains the shared async-run queues (audit/executor/
  # invitation). A staging drainer would execute runs restored from the prod snapshot using the
  # third-party credentials this service holds.
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging;    MIN=1; MAX=4;  CPU=2; MEM=2Gi; WORKERS=false; POOL_MIN=2; POOL_MAX=8 ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production; MIN=2; MAX=20; CPU=2; MEM=4Gi; WORKERS=true;  POOL_MIN=2; POOL_MAX=6 ;;
  *) echo "bad env" >&2; exit 2 ;;
esac
HERE="$(cd "$(dirname "$0")" && pwd)"
# Staging holds a restored copy of production data and production third-party credentials, so it is
# IAM-gated by default. Prod is a public API. Override with PUBLIC=1 / PUBLIC=0.
: "${PUBLIC:=$([ "$ENV" = prod ] && echo 1 || echo 0)}"
[ "$PUBLIC" = 1 ] && PUBLIC_FLAG=--allow-unauthenticated || PUBLIC_FLAG=--no-allow-unauthenticated
GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
SERVICE="${SERVICE:-web}"
IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/backend:$TAG"
CANDIDATE_TAG="c-$(printf '%s' "$TAG" | tr -cd '[:alnum:]' | tail -c 12)"
ENV_FILE="$HERE/env.$ENV.yaml"
SECRETS_FILE="$HERE/secrets.$ENV.list"
[ -f "$ENV_FILE" ] && [ -f "$SECRETS_FILE" ] || { echo "missing $ENV_FILE / $SECRETS_FILE — run port_railway_env.py first" >&2; exit 1; }

# Secret Manager mappings: DATABASE_URL/REDIS_URL from bootstrap + every env-* secret
SECRETS="DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest,$(paste -sd, "$SECRETS_FILE")"

# gcloud allows only ONE env-vars flag: merge the ported file with the platform vars into a temp file
MERGED=$(mktemp); chmod 600 "$MERGED"; trap 'rm -f "$MERGED"' EXIT INT TERM
# NOTE: port_railway_env.py drops RAILWAY_*, but several gates in this codebase still read those
# names directly and FAIL-SAFE TOWARD ON when they are absent (utils/startup_mode.py:14 heavy startup,
# services/audit_scheduler.py:_queue_worker_enabled). On Cloud Run that would re-arm the empty-DB DDL
# race on every autoscale event and run the queue drainers on every instance. Set the explicit
# overrides those gates check first. Remove once config/platform.py (#1771) covers every reader.
{ cat "$ENV_FILE"
  printf 'PIVOTA_ENV: "%s"\nPIVOTA_SERVICE_NAME: "%s"\nPIVOTA_COMMIT_SHA: "%s"\nPIVOTA_PLATFORM: "cloud_run"\n' "$PIVOTA_ENV" "$SERVICE" "$TAG"
  printf 'SKIP_HEAVY_STARTUP_INIT: "true"\n'
  printf 'AUDIT_WORKER_ENABLED: "%s"\n' "$WORKERS"
  printf 'REVIEWS_INVITATION_WORKER_ENABLED: "%s"\n' "$WORKERS"
  # Cloud SQL max_connections=200 (bootstrap_env.sh). db/database.py defaults to a 5..20 pool PER
  # PROCESS, so MAX instances x 20 would be 400 on prod and exhaust the server. Size the pool from
  # the instance ceiling, leaving headroom for the other services and for ops sessions.
  printf 'DB_POOL_MIN_SIZE: "%s"\nDB_POOL_MAX_SIZE: "%s"\n' "$POOL_MIN" "$POOL_MAX"
} > "$MERGED"

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
  $PUBLIC_FLAG \
  --labels "env=$ENV,service=$SERVICE,managed-by=infra-gcp" \
  --tag "$CANDIDATE_TAG" --no-traffic \
  --quiet

# Verify the candidate revision on its own tagged URL BEFORE it takes traffic: `gcloud run deploy`
# reports success as soon as the container passes its startup probe, which a revision that boots
# but cannot serve still does.
CAND_URL=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format="value(status.traffic.extract(\"url\").flatten())" | tr ';' '\n' | grep -F "$CANDIDATE_TAG" | head -1)
CAND_URL="${CAND_URL:-$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')}"
AUTH=()
[ "$PUBLIC" = 1 ] || AUTH=(-H "Authorization: Bearer $("$GCLOUD" auth print-identity-token)")
echo "verifying candidate at $CAND_URL"
CODE=$(curl -sS -o /tmp/pivota-health.$$ -w '%{http_code}' -m 30 "${AUTH[@]}" "$CAND_URL/health" || echo 000)
head -c 400 /tmp/pivota-health.$$; echo; rm -f /tmp/pivota-health.$$
[ "$CODE" = 200 ] || { echo "candidate health check returned $CODE — NOT shifting traffic. Previous revision still serving." >&2; exit 1; }

"$GCLOUD" run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --to-latest --quiet
URL=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
echo "deployed $SERVICE -> $URL (100% traffic)"
