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
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging;    MIN=1; MAX=4;  CPU=2; MEM=2Gi; GW_POOL_MAIN=3; GW_POOL_AUX=2 ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production; MIN=2; MAX=20; CPU=2; MEM=4Gi; GW_POOL_MAIN=2; GW_POOL_AUX=1 ;;
  *) echo "bad env" >&2; exit 2 ;;
esac
HERE="$(cd "$(dirname "$0")" && pwd)"
# all-traffic, NOT private-ranges-only. Under private-ranges-only outbound traffic to the public
# internet does not traverse the VPC, so it never leaves via Cloud NAT and the reserved address is
# NOT the source IP. `8.231.167.230` is published to Antom/Adyen for allowlisting, so a deploy that
# reverted this would silently break their IP checks. Verified from inside the VPC: a Cloud Run job
# on this egress mode reports EGRESS_IP=8.231.167.230.
: "${VPC_EGRESS:=all-traffic}"
# `internal` alone does NOT admit the load balancer - only internal-and-cloud-load-balancing does,
# so prod (which sits behind the LB) must use the latter.
# Staging defaults to `all` deliberately: the live staging gateway is reachable on its run.app URL
# today, and silently flipping a SERVICE-level setting mid-deploy would take it offline for anything
# outside the VPC - including the operator running the deploy. Pass INGRESS=internal explicitly to
# tighten it as a considered change rather than a side effect of shipping code.
: "${INGRESS:=$([ "$ENV" = prod ] && echo internal-and-cloud-load-balancing || echo all)}"
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
# Strip exactly the keys re-set below. DB_POOL_MIN_SIZE/DB_POOL_MAX_SIZE are kept in this list only
# because a ported Railway env may still carry them (they are inert here); the four names that DO
# matter must be stripped, or a ported value would win - gcloud takes the FIRST duplicate key - and
# the printf below would be silently inert, reverting the gateway to 5+3+3+3 per instance.
grep -vE '^(PIVOTA_ENV|PIVOTA_SERVICE_NAME|PIVOTA_COMMIT_SHA|PIVOTA_PLATFORM|SKIP_HEAVY_STARTUP_INIT|AUDIT_WORKER_ENABLED|REVIEWS_INVITATION_WORKER_ENABLED|DB_POOL_MIN_SIZE|DB_POOL_MAX_SIZE|DB_POOL_MAX|PCI_KB_DB_POOL_MAX|INGREDIENT_REFERENCE_DB_POOL_MAX|INGREDIENT_SIGNAL_DB_POOL_MAX):' "$ENV_FILE" > "$MERGED"
{ :
  # requirePlatformEnv() throws at boot without this (src/server.js:52114).
  printf 'PIVOTA_ENV: "%s"\nPIVOTA_SERVICE_NAME: "%s"\nPIVOTA_COMMIT_SHA: "%s"\n' "$PIVOTA_ENV" "$SERVICE" "$TAG"
  # The gateway opens FOUR pools, not one, and reads these exact names:
  #   DB_POOL_MAX                      src/db/index.js:132                 (default 5)
  #   PCI_KB_DB_POOL_MAX               src/services/pciKbClient.js:46      (default 3)
  #   INGREDIENT_REFERENCE_DB_POOL_MAX src/services/ingredientReferenceStore.js:82 (default 3)
  #   INGREDIENT_SIGNAL_DB_POOL_MAX    src/services/ingredientSignalStore.js:93    (default 3)
  # Defaults therefore sum to 14 connections PER INSTANCE, not 10. PG_POOL_MAX / PGPOOL_MAX /
  # DB_POOL_MAX_SIZE - which an earlier version of this script set - are read by NOTHING in this
  # repo, so that sizing was inert. Measure the variable names before trusting a budget.
  #
  # BUDGET against max_connections=300 (raised from 200):
  #   web 20x6=120 + gateway 20x5=100 + worker 1x10=10 = 230, leaving 70 for ops and superuser.
  #   proof-issuer and acp mount no DATABASE_URL at all, so they contribute 0.
  printf 'DB_POOL_MAX: "%s"\nPCI_KB_DB_POOL_MAX: "%s"\nINGREDIENT_REFERENCE_DB_POOL_MAX: "%s"\nINGREDIENT_SIGNAL_DB_POOL_MAX: "%s"\n' \
    "$GW_POOL_MAIN" "$GW_POOL_AUX" "$GW_POOL_AUX" "$GW_POOL_AUX"
} >> "$MERGED"

# The candidate gate must be able to REACH the candidate. Every prod service is
# `internal-and-cloud-load-balancing`, so a curl from an operator's laptop gets Google's 404 before
# the container is ever consulted - the gate would then exit 1 on every prod deploy and strand a
# perfectly good revision at 0%. Measured 2026-08-20: web/gateway/proof-issuer/acp all 404 from
# outside; `internal` 404s before IAM, `all` gets as far as a 403.
#
# So: try directly, and if the answer is an ingress/IAM rejection rather than the app, re-probe from
# INSIDE the VPC with a one-shot Cloud Run job. Only a real 200 from the application passes.
probe_health(){ # url -> echoes the status code
  local url="$1" code
  code=$(curl -sS -o /tmp/pivota-health.$$ -w '%{http_code}' -m 30 ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} "$url" 2>/dev/null || echo 000)
  case "$code" in
    200) rm -f /tmp/pivota-health.$$; echo 200; return 0 ;;
    403|404|000) : ;;                     # possibly ingress/IAM, not the app - fall through
    *) rm -f /tmp/pivota-health.$$; echo "$code"; return 0 ;;
  esac
  rm -f /tmp/pivota-health.$$
  echo "   direct probe got $code (ingress-blocked from here); re-probing from inside the VPC" >&2
  # ^|^ delimiter: gcloud splits --args on COMMAS, and this probe is Python that contains commas
  # (`,timeout=25`), which would otherwise be shredded into separate argv entries.
  local job="verify-$$-$RANDOM"
  "$GCLOUD" run jobs create "$job" --region "$REGION" --project "$PROJECT" \
    --image "$REGION-docker.pkg.dev/pivota-shared/pivota/backend:latest" \
    --service-account "sa-worker@$PROJECT.iam.gserviceaccount.com" \
    --network default --subnet default --vpc-egress all-traffic \
    --max-retries 0 --task-timeout 120s --command python \
    --args="^|^-c|import urllib.request;print('PROBE_STATUS='+str(urllib.request.urlopen('$url',timeout=25).status))" \
    --quiet >/dev/null 2>&1
  local out="" i
  if "$GCLOUD" run jobs execute "$job" --region "$REGION" --project "$PROJECT" --wait --quiet >/dev/null 2>&1; then
    # Cloud Logging ingestion lags the job's exit by a few seconds. Reading immediately returns
    # nothing and the probe reports 000 - which reads exactly like a failed health check and would
    # strand a healthy revision. Poll instead of guessing a sleep.
    for i in 1 2 3 4 5 6; do
      out=$("$GCLOUD" logging read "resource.labels.job_name=\"$job\"" --project "$PROJECT" --limit 15 \
        --format='value(textPayload)' 2>/dev/null | grep -oE 'PROBE_STATUS=[0-9]+' | head -1 | cut -d= -f2)
      [ -n "$out" ] && break
      sleep 5
    done
  fi
  "$GCLOUD" run jobs delete "$job" --region "$REGION" --project "$PROJECT" --quiet >/dev/null 2>&1 || true
  echo "${out:-000}"
}

"$GCLOUD" run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "sa-gateway@$PROJECT.iam.gserviceaccount.com" \
  --network default --subnet default --vpc-egress "$VPC_EGRESS" \
  --env-vars-file "$MERGED" \
  --set-secrets "$SECRETS" \
  --port 8080 --cpu "$CPU" --memory "$MEM" --concurrency 80 --timeout 300 \
  --min-instances "${MIN_INSTANCES:-$MIN}" --max-instances "${MAX_INSTANCES:-$MAX}" \
  --no-cpu-throttling --cpu-boost --execution-environment gen2 \
  --ingress "$INGRESS" \
  $PUBLIC_FLAG \
  --labels "env=$ENV,service=$SERVICE,managed-by=infra-gcp" \
  $NO_TRAFFIC \
  --quiet

CAND_URL=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format="value(status.traffic.extract(\"url\").flatten())" | tr ',;' '\n\n' | grep -F "$CANDIDATE_TAG" | head -1)
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
CODE=$(probe_health "$CAND_URL/health")
[ "$CODE" = 200 ] || { echo "candidate /health returned $CODE - NOT shifting traffic." >&2; exit 1; }

# Retire stale candidate tags, and say what was retired.
#
# WHY THIS IS NOT COSMETIC. A tagged revision is never garbage-collected, and every service
# here sets min-instances >= 1, so each old candidate keeps instances RUNNING forever. Cloud Run
# resolves `--set-secrets ...:latest` at INSTANCE START, not per request, so those immortal
# instances stay pinned to whatever secret VERSION was latest when they booted.
#
# That is not hypothetical. After the 2026-08-22 cutover, `gateway-00010-mar` (booted 08-20, so
# holding DATABASE_URL_NOVERIFY v1) kept running its in-process pdp_identity_auto_resolve timer
# every 30 minutes against the RETIRED `pivota` database - including 400 rows written AFTER the
# secret had been repointed to the cutover snapshot. Repointing a secret fixes the revision that
# takes traffic; it does nothing to the ones still pinned behind a tag.
#
# Only tags on revisions serving 0% are removed; the live revision keeps its own tag. Parsed from
# JSON rather than `--format=filter("percent:0")` - that filter currently matches the 100%-traffic
# entry as well (gcloud warns its operator semantics are changing), which would untag the LIVE
# revision. Cleanup failure is reported but never fails the deploy: the promotion already
# succeeded, and leaving a stale tag is worse-but-not-broken.
sweep_stale_tags() {
  local stale
  stale=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format=json 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print(",".join(t["tag"] for t in d.get("status",{}).get("traffic",[])
                if t.get("tag") and not t.get("percent")))' 2>/dev/null) || return 0
  [ -n "$stale" ] || { echo "no stale candidate tags"; return 0; }
  echo "retiring stale candidate tags: $stale"
  "$GCLOUD" run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --remove-tags="$stale" --quiet >/dev/null 2>&1 \
    || echo "WARNING: could not remove stale tags ($stale) - remove them by hand, they keep instances alive on old secret versions" >&2
}

[ "$FIRST_DEPLOY" = 1 ] || "$GCLOUD" run services update-traffic "$SERVICE" --project "$PROJECT" --region "$REGION" --to-latest --quiet
sweep_stale_tags
echo "deployed $SERVICE -> $("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)') (100% traffic)"
