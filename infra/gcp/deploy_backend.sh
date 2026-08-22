#!/usr/bin/env bash
# Deploy the backend image to Cloud Run in one environment.
#   infra/gcp/deploy_backend.sh staging|prod <image-tag>   (tag = git sha pushed by cloudbuild.backend.yaml)
# Prereqs: bootstrap_env.sh ran; port_railway_env.py --apply ran (env.<env>.yaml + secrets.<env>.list exist).
set -euo pipefail
ENV="${1:-}"; TAG="${2:-}"
[ -n "$ENV" ] && [ -n "$TAG" ] || { echo "usage: $0 staging|prod <image-tag>" >&2; exit 2; }
case "$ENV" in
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging;    MIN=1; MAX=4;  CPU=2; MEM=2Gi; POOL_MIN=2; POOL_MAX=8 ;;
  prod)    PROJECT=pivota-prod;    PIVOTA_ENV=production; MIN=2; MAX=20; CPU=2; MEM=4Gi; POOL_MIN=2; POOL_MAX=6 ;;
  *) echo "bad env" >&2; exit 2 ;;
esac

# WORKERS must be forced OFF for the whole pre-cutover window. Between now and the DNS flip the GCP
# prod stack runs against a COPY of production data with the REAL production third-party credentials
# while Railway prod is still serving. Two live drainers on two copies of the same queue means
# duplicate emails, duplicate settlement attempts, duplicate captures. Cutover flips this to true as
# a deliberate step, AFTER Railway's workers are stopped.
#   pre-cutover:  WORKERS=false infra/gcp/deploy_backend.sh prod <tag>   (default below)
#   at cutover:   WORKERS=true  infra/gcp/deploy_backend.sh prod <tag>
: "${WORKERS:=false}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Staging holds a restored copy of production data and production third-party credentials, so it is
# IAM-gated by default. Prod is a public API. Override with PUBLIC=1 / PUBLIC=0.
# all-traffic, NOT private-ranges-only. Under private-ranges-only outbound traffic to the public
# internet does not traverse the VPC, so it never leaves via Cloud NAT and the reserved address is
# NOT the source IP. `8.231.167.230` is published to Antom/Adyen for allowlisting, so a deploy that
# reverted this would silently break their IP checks. Verified from inside the VPC: a Cloud Run job
# on this egress mode reports EGRESS_IP=8.231.167.230.
: "${VPC_EGRESS:=all-traffic}"
: "${PUBLIC:=$([ "$ENV" = prod ] && echo 1 || echo 0)}"
# `internal` and `internal-and-cloud-load-balancing` are DIFFERENT values: only the latter admits
# requests from Google Cloud Load Balancing. Setting plain `internal` on a service behind the LB
# makes every request through api.pivota.cc fail with a valid certificate and a correct-looking
# url map - the same "looks built, is not" shape as the unattached backend service.
: "${INGRESS:=$([ "$ENV" = prod ] && echo internal-and-cloud-load-balancing || echo internal)}"
[ "$PUBLIC" = 1 ] && PUBLIC_FLAG=--allow-unauthenticated || PUBLIC_FLAG=--no-allow-unauthenticated
GCLOUD="${GCLOUD:-gcloud}"
REGION=us-west1
SERVICE="${SERVICE:-web}"
# Reusable for the other Python services that ship from this repo or their own image:
#   IMAGE_NAME  which Artifact Registry image to run (backend | acp | ...)
#   ENV_PREFIX  which ported env/secrets files to use (empty = the backend's)
#   RUN_COMMAND/RUN_ARGS  override the image entrypoint (proof-issuer is the same backend image
#               with a different ASGI app, so it needs no image of its own)
: "${IMAGE_NAME:=backend}"
: "${ENV_PREFIX:=}"
# A prefix TYPO fails closed on the -f check below. A prefix OMISSION does not: it silently hands
# another service the backend's entire env file and secret list. Require them to agree.
case "$SERVICE" in
  web) [ -z "$ENV_PREFIX" ] || { echo "SERVICE=web must not set ENV_PREFIX (got '$ENV_PREFIX')" >&2; exit 2; } ;;
  *)   [ -n "$ENV_PREFIX" ] || { echo "SERVICE=$SERVICE requires ENV_PREFIX (else it deploys with the BACKEND's config)" >&2; exit 2; } ;;
esac
IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/$IMAGE_NAME:$TAG"
CANDIDATE_TAG="c-$(printf '%s' "$TAG" | tr -cd '[:alnum:]' | tail -c 12)"
# `--no-traffic` is rejected on service CREATION, so the candidate-then-verify flow only applies to
# an existing service. A brand-new service has no previous revision to protect anyway.
if "$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  NO_TRAFFIC="--tag $CANDIDATE_TAG --no-traffic"; FIRST_DEPLOY=0
else
  NO_TRAFFIC=""; FIRST_DEPLOY=1; echo "note: $SERVICE does not exist yet - first revision takes traffic immediately"
fi
TAGPART="${ENV_PREFIX:+.$ENV_PREFIX}"
ENV_FILE="$HERE/env.$ENV$TAGPART.yaml"
SECRETS_FILE="$HERE/secrets.$ENV$TAGPART.list"
[ -f "$ENV_FILE" ] && [ -f "$SECRETS_FILE" ] || { echo "missing $ENV_FILE / $SECRETS_FILE — run port_railway_env.py first" >&2; exit 1; }

# Secret Manager mappings: DATABASE_URL/REDIS_URL from bootstrap + every env-* secret
# Mounting the datastore DSNs is OPT-IN, not automatic. Handing a service a DATABASE_URL it never
# had does not just add connections - it can change BEHAVIOUR. acp is the proof: on Railway it has no
# DATABASE_URL and runs on its in-memory fallback, but the unconditional mount made the Cloud Run copy
# log "ACP ready with database persistence" and open an asyncpg pool. Its `Database(DATABASE_URL)`
# takes NO pool arguments, so it is asyncpg's default min=10/max=10 per instance and it honours
# neither DB_POOL_MAX_SIZE nor DATABASE_POOL_SIZE - 10 x 20 instances = 200 connections on its own,
# against a max_connections of 200, plus a silent persistence change in the payment path.
: "${MOUNT_DB:=$([ "$SERVICE" = web ] || [ "$SERVICE" = worker ] && echo 1 || echo 0)}"
# Validate, like WORKERS/PAUSED elsewhere. MOUNT_DB=true - the spelling those flags use - would
# otherwise evaluate false and deploy `web` with NO DATABASE_URL, which fails at runtime, not here.
case "$MOUNT_DB" in 0|1) ;; *) echo "MOUNT_DB must be exactly 0 or 1 (got '$MOUNT_DB')" >&2; exit 2 ;; esac
DB_SECRETS=""
[ "$MOUNT_DB" = 1 ] && DB_SECRETS="DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest,"
SECRETS="${DB_SECRETS}$(paste -sd, "$SECRETS_FILE")"

# gcloud allows only ONE env-vars flag: merge the ported file with the platform vars into a temp file
# Pass these UNCONDITIONALLY. Setting them only when non-empty means a RUN_COMMAND left exported
# from a previous service's deploy rides along into the next one - and because proof_issuer_main also
# serves /health, the wrong application would PASS the candidate check and take 100% of traffic on
# api.pivota.cc. An empty value is gcloud's documented reset, so this also clears a stale override.
# --flag=value form: RUN_ARGS legitimately starts with a dash ("-m,uvicorn,...") and argparse would
# otherwise read it as the next flag.
[ -n "${RUN_ARGS:-}" ] && [ -z "${RUN_COMMAND:-}" ] && {
  echo "RUN_ARGS without RUN_COMMAND produces an unbootable revision (the image has no ENTRYPOINT)." >&2; exit 2; }
CMD_ARGS=("--command=${RUN_COMMAND:-}" "--args=${RUN_ARGS:-}")

MERGED=$(mktemp); chmod 600 "$MERGED"; trap 'rm -f "$MERGED"' EXIT INT TERM
# NOTE: port_railway_env.py drops RAILWAY_*, but several gates in this codebase still read those
# names directly and FAIL-SAFE TOWARD ON when they are absent (utils/startup_mode.py:14 heavy startup,
# services/audit_scheduler.py:_queue_worker_enabled). On Cloud Run that would re-arm the empty-DB DDL
# race on every autoscale event and run the queue drainers on every instance. Set the explicit
# overrides those gates check first. Remove once config/platform.py (#1771) covers every reader.
# gcloud's --env-vars-file resolves a DUPLICATE KEY to the FIRST occurrence, not the last (verified
# against the SDK's own loader). Appending an override after the ported file therefore does NOTHING
# whenever the ported file already defines that key - which silently made WORKERS, DB_POOL_* and even
# PIVOTA_ENV inert. Strip the keys we are about to set before appending them.
grep -vE '^(PIVOTA_ENV|PIVOTA_SERVICE_NAME|PIVOTA_COMMIT_SHA|PIVOTA_PLATFORM|SKIP_HEAVY_STARTUP_INIT|AUDIT_WORKER_ENABLED|REVIEWS_INVITATION_WORKER_ENABLED|DB_POOL_MIN_SIZE|DB_POOL_MAX_SIZE):' "$ENV_FILE" > "$MERGED"
{ :
  printf 'PIVOTA_ENV: "%s"\nPIVOTA_SERVICE_NAME: "%s"\nPIVOTA_COMMIT_SHA: "%s"\nPIVOTA_PLATFORM: "cloud_run"\n' "$PIVOTA_ENV" "$SERVICE" "$TAG"
  printf 'SKIP_HEAVY_STARTUP_INIT: "true"\n'
  printf 'AUDIT_WORKER_ENABLED: "%s"\n' "$WORKERS"
  printf 'REVIEWS_INVITATION_WORKER_ENABLED: "%s"\n' "$WORKERS"
  # Cloud SQL max_connections=300 (bootstrap_env.sh). db/database.py defaults to a 5..20 pool PER
  # PROCESS, so MAX instances x 20 would be 400 on prod and exhaust the server. Size the pool from
  # the instance ceiling, leaving headroom for the other services and for ops sessions.
  printf 'DB_POOL_MIN_SIZE: "%s"\nDB_POOL_MAX_SIZE: "%s"\n' "$POOL_MIN" "$POOL_MAX"
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
  --service-account "${SERVICE_ACCOUNT:-sa-backend}@$PROJECT.iam.gserviceaccount.com" \
  --network default --subnet default --vpc-egress "$VPC_EGRESS" \
  --env-vars-file "$MERGED" \
  --set-secrets "$SECRETS" \
  ${CMD_ARGS[@]+"${CMD_ARGS[@]}"} \
  --port 8080 --cpu "$CPU" --memory "$MEM" --concurrency 80 --timeout 300 \
  --min-instances "${MIN_INSTANCES:-$MIN}" --max-instances "${MAX_INSTANCES:-$MAX}" \
  --no-cpu-throttling --cpu-boost \
  --execution-environment gen2 \
  --ingress "$INGRESS" \
  $PUBLIC_FLAG \
  --labels "env=$ENV,service=$SERVICE,managed-by=infra-gcp" \
  $NO_TRAFFIC \
  --quiet

# Verify the candidate revision on its own tagged URL BEFORE it takes traffic: `gcloud run deploy`
# reports success as soon as the container passes its startup probe, which a revision that boots
# but cannot serve still does.
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
CODE=$(probe_health "$CAND_URL/health")
[ "$CODE" = 200 ] || { echo "candidate health check returned $CODE — NOT shifting traffic. Previous revision still serving." >&2; exit 1; }

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
URL=$("$GCLOUD" run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
echo "deployed $SERVICE -> $URL (100% traffic)"
