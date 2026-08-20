#!/usr/bin/env bash
# Deploy the PIVOTA-Agent gateway image to Cloud Run in one environment.
#   infra/gcp/deploy_gateway.sh staging|prod <image-tag>
# Prereqs: bootstrap_env.sh ran; port_railway_env.py --apply produced env.<env>.gateway.yaml and
# secrets.<env>.gateway.list (pass --prefix gateway when porting the gateway's Railway service).
#
# The gateway image comes from the PIVOTA-Agent repo's own root Dockerfile (node:20-bookworm-slim,
# non-root `node`, CMD node src/server.js). That repo's Railway services are pinned to the RAILPACK
# builder explicitly, so the root Dockerfile is inert there - unlike pivota-backend, where adding one
# hijacked the builder for 8 services.
set -euo pipefail
ENV="${1:-}"; TAG="${2:-}"
[ -n "$ENV" ] && [ -n "$TAG" ] || { echo "usage: $0 staging|prod <image-tag>" >&2; exit 2; }
case "$ENV" in
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging;    MIN=1; MAX=4;  CPU=2; MEM=2Gi; PG_POOL_MAX=5 ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production; MIN=2; MAX=20; CPU=2; MEM=4Gi; PG_POOL_MAX=3 ;;
  *) echo "bad env" >&2; exit 2 ;;
esac
HERE="$(cd "$(dirname "$0")" && pwd)"
# all-traffic, NOT private-ranges-only. Under private-ranges-only outbound traffic to the public
# internet does not traverse the VPC, so it never leaves via Cloud NAT and the reserved address is
# NOT the source IP. `8.231.167.230` is published to Antom/Adyen for allowlisting, so a deploy that
# reverted this would silently break their IP checks. Verified from inside the VPC: a Cloud Run job
# on this egress mode reports EGRESS_IP=8.231.167.230.
: "${VPC_EGRESS:=all-traffic}"
# `internal` alone does NOT admit the load balancer - only internal-and-cloud-load-balancing does.
: "${INGRESS:=$([ "$ENV" = prod ] && echo internal-and-cloud-load-balancing || echo internal)}"
: "${PUBLIC:=$([ "$ENV" = prod ] && echo 1 || echo 0)}"
[ "$PUBLIC" = 1 ] && PUBLIC_FLAG=--allow-unauthenticated || PUBLIC_FLAG=--no-allow-unauthenticated
GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
SERVICE="${SERVICE:-gateway}"
IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/gateway:$TAG"
CANDIDATE_TAG="c-$(printf '%s' "$TAG" | tr -cd '[:alnum:]' | tail -c 12)"
# `--no-traffic` is rejected on service CREATION, so the candidate-then-verify flow only applies to
# an existing service. A brand-new service has no previous revision to protect anyway.
if "$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  NO_TRAFFIC="--tag $CANDIDATE_TAG --no-traffic"; FIRST_DEPLOY=0
else
  NO_TRAFFIC=""; FIRST_DEPLOY=1; echo "note: $SERVICE does not exist yet - first revision takes traffic immediately"
fi
ENV_FILE="$HERE/env.$ENV.gateway.yaml"
SECRETS_FILE="$HERE/secrets.$ENV.gateway.list"
[ -f "$ENV_FILE" ] && [ -f "$SECRETS_FILE" ] || { echo "missing $ENV_FILE / $SECRETS_FILE - run port_railway_env.py --prefix gateway first" >&2; exit 1; }

# The gateway talks to Postgres directly (internal_catalog / external_seeds discovery providers), so
# it needs DATABASE_URL and the pci_kb DSN - neither of which the porter emits: it DROPS DATABASE_URL
# by design, because the value must come from Cloud SQL rather than Railway. Mount them explicitly,
# or /health reports db_backed_providers_ready=false with code "missing_database" and discovery runs
# single-provider without ever erroring.
#
# The _NOVERIFY variants exist because node-pg validates the server certificate chain differently to
# asyncpg against Cloud SQL's private IP; the Python services use the plain sslmode=require DSNs.
SECRETS="DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest,INGREDIENT_REFERENCE_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest,$(paste -sd, "$SECRETS_FILE")"
MERGED=$(mktemp); chmod 600 "$MERGED"; trap 'rm -f "$MERGED"' EXIT INT TERM
# gcloud's --env-vars-file resolves a DUPLICATE KEY to the FIRST occurrence, not the last (verified
# against the SDK's own loader). Appending an override after the ported file therefore does NOTHING
# whenever the ported file already defines that key - which silently made WORKERS, DB_POOL_* and even
# PIVOTA_ENV inert. Strip the keys we are about to set before appending them.
grep -vE '^(PIVOTA_ENV|PIVOTA_SERVICE_NAME|PIVOTA_COMMIT_SHA|PIVOTA_PLATFORM|SKIP_HEAVY_STARTUP_INIT|AUDIT_WORKER_ENABLED|REVIEWS_INVITATION_WORKER_ENABLED|DB_POOL_MIN_SIZE|DB_POOL_MAX_SIZE):' "$ENV_FILE" > "$MERGED"
{ :
  # requirePlatformEnv() throws at boot without this (src/server.js:52114).
  printf 'PIVOTA_ENV: "%s"\nPIVOTA_SERVICE_NAME: "%s"\nPIVOTA_COMMIT_SHA: "%s"\n' "$PIVOTA_ENV" "$SERVICE" "$TAG"
  # node-pg's Pool defaults to 10 connections PER PROCESS. At --max-instances 20 that is 200 on its
  # own, and Cloud SQL max_connections is 200 shared with web and worker. Staging (MAX=4) can never
  # reveal this. Budget: gateway 20x3=60 + web 20x6=120 + worker 10 = 190, under 200.
  printf 'PG_POOL_MAX: "%s"\nPGPOOL_MAX: "%s"\nDB_POOL_MAX_SIZE: "%s"\n' "$PG_POOL_MAX" "$PG_POOL_MAX" "$PG_POOL_MAX"
} >> "$MERGED"

"$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "sa-gateway@$PROJECT.iam.gserviceaccount.com" \
  --network default --subnet default --vpc-egress "$VPC_EGRESS" \
  --env-vars-file "$MERGED" \
  --set-secrets "$SECRETS" \
  --port 8080 --cpu "$CPU" --memory "$MEM" --concurrency 80 --timeout 300 \
  --min-instances "$MIN" --max-instances "$MAX" \
  --no-cpu-throttling --cpu-boost --execution-environment gen2 \
  --ingress "$INGRESS" \
  $PUBLIC_FLAG \
  --labels "env=$ENV,service=$SERVICE,managed-by=infra-gcp" \
  $NO_TRAFFIC \
  --quiet

CAND_URL=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format="value(status.traffic.extract(\"url\").flatten())" | tr ';' '\n' | grep -F "$CANDIDATE_TAG" | head -1)
[ "$FIRST_DEPLOY" = 1 ] && CAND_URL=""
CAND_URL="${CAND_URL:-$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')}"
# macOS ships bash 3.2, where "${AUTH[@]}" on an EMPTY array is an unbound-variable error under
# `set -u` — which is exactly the public (PUBLIC=1) case. Use the ${arr[@]+"${arr[@]}"} guard.
AUTH=()
[ "$PUBLIC" = 1 ] || AUTH=(-H "Authorization: Bearer $("$GCLOUD" auth print-identity-token)")
AUTH_ARGS=(${AUTH[@]+"${AUTH[@]}"})
echo "verifying candidate at $CAND_URL"
# /health, NOT /healthz: Cloud Run's frontend intercepts /healthz and returns its own 404 before the
# request reaches the container (proved 2026-08-19: /health -> 200 application/json from the app,
# /healthz -> 404 text/html from GFE). Railway's healthcheckPath is /healthz, so this differs by platform.
CODE=$(curl -sS -o /tmp/pivota-gw-health.$$ -w '%{http_code}' -m 30 ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "$CAND_URL/health" || echo 000)
head -c 400 /tmp/pivota-gw-health.$$; echo; rm -f /tmp/pivota-gw-health.$$
[ "$CODE" = 200 ] || { echo "candidate /health returned $CODE - NOT shifting traffic." >&2; exit 1; }

[ "$FIRST_DEPLOY" = 1 ] || "$GCLOUD" run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --to-latest --quiet
echo "deployed $SERVICE -> $("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)') (100% traffic)"
