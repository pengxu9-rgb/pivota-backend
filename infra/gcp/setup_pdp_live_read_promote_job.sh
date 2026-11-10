#!/usr/bin/env bash
# Provision the PDP identity live-read promote as a NAMED Cloud Run Job (`pdp-live-read-promote`,
# gateway image) with NO Cloud Scheduler trigger.
#
#   infra/gcp/setup_pdp_live_read_promote_job.sh staging|prod <gateway-tag>
#
# FOR THE OPERATOR. Not run by CI or by any deploy script. Runbook: docs/runbooks/catalog_jobs_on_gcp.md.
#
# WHAT IT RUNS. PIVOTA-Agent scripts/promote-pdp-identity-live-read.js: turns on live_read_enabled
# for PDP identity groups (and, through the identity-event trust refresh, writes catalog_row_trust).
# Until now each cohort was promoted from a laptop, through a one-off job built around a local copy
# of the script. This gives that step a fixed, reviewable job definition that runs from Cloud Shell
# or the console, so no laptop is in the path.
#
# WHY THERE IS NO TRIGGER, AND NO --enable. Each promote is a per-cohort decision (promoting
# merchant-only groups is an ADR-009 policy override, and that call belongs to the owner), so nothing
# runs it on a timer. The job's own args are a DRY RUN: an execution started without overriding the
# args writes nothing and prints what it would promote. A write is an explicit override, e.g.
#
#   gcloud run jobs execute pdp-live-read-promote --region us-west1 --project pivota-prod --wait \
#     --args="scripts/promote-pdp-identity-live-read.js,--brand,<brand>,--limit,500,--created-by,<you>,--write"
#
# (`--args` REPLACES the job's args entirely; without `--write` the override is still a dry run.)
#
# PINNED ENV, VERIFIED AGAINST THE GATEWAY. The script says it "must run with the same switch as
# both services" (CATALOG_TRUST_CRAWL_FRESH_*), and the identity graph reads its own flags. Each one
# is pinned below to the live gateway's value, with `<unset>` meaning the gateway does not set it and
# neither does this job; the preflight REFUSES to provision on any difference.
#
# THIS SCRIPT OWNS THE JOB'S WHOLE ENV AND SECRETS: --set-env-vars / --set-secrets REPLACE the set on
# every run, so a value added by hand is wiped by the next run. Pin it here instead.
#
# Default subnet: Cloud SQL only, no merchant fetches.
set -euo pipefail

ENV="${1:-}"; GATEWAY_TAG="${2:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <gateway-tag>" >&2; exit 2 ;;
esac
[ -n "$GATEWAY_TAG" ] || { echo "gateway-tag is required" >&2; exit 2; }
case "$GATEWAY_TAG" in
  latest) echo "refusing the floating tag 'latest' - pass the commit sha" >&2; exit 2 ;;
esac
[ "$#" -le 2 ] || { echo "too many arguments (this job has no trigger, so there is no --enable)" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"; PYTHON="${PYTHON:-python3}"; REGION=us-west1
JOB=pdp-live-read-promote
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
have(){ "$@" >/dev/null 2>&1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"
# shellcheck source=infra/gcp/_gateway_image.sh
. "$(cd "$(dirname "$0")" && pwd)/_gateway_image.sh"

PINNED_FLAGS="PDP_IDENTITY_GRAPH_ENABLED=true
PDP_IDENTITY_GRAPH_BRAND_ALLOWLIST=kravebeauty
PDP_IDENTITY_GRAPH_AUTO_ENABLE_LIVE=<unset>
CATALOG_TRUST_CRAWL_FRESH_ENABLED=<unset>
CATALOG_TRUST_CRAWL_FRESH_DAYS=<unset>
SHADE_LINE_CARD_COLLAPSE_ENABLED=<unset>"

have "$GCLOUD" iam service-accounts describe "$SA" || { echo "missing $SA" >&2; exit 1; }
GATEWAY_IMAGE="$(resolve_gateway_image "$GATEWAY_TAG")" \
  || { echo "missing image gateway:$GATEWAY_TAG in pivota-gateway and pivota; build it first" >&2; exit 1; }

echo "== preflight: pinned flags vs the live gateway service"
GATEWAY_JSON="$("$GCLOUD" run services describe gateway --region "$REGION" --format=json)" \
  || { echo "cannot read the gateway service; refusing to provision without verifying the pinned flags" >&2; exit 1; }
GATEWAY_JSON="$GATEWAY_JSON" PINNED_FLAGS="$PINNED_FLAGS" "$PYTHON" - <<'PY' || exit 1
import json, os, sys
doc = json.loads(os.environ["GATEWAY_JSON"])
env = {}
for e in doc["spec"]["template"]["spec"]["containers"][0].get("env", []):
    env[e["name"]] = e.get("value") if "value" in e else None
bad = []
for line in os.environ["PINNED_FLAGS"].splitlines():
    name, want = line.split("=", 1)
    have = env.get(name, "<unset>")
    if have != want:
        bad.append(f"  {name}: pinned {want!r}, gateway has {have!r}")
if bad:
    print("REFUSING: pinned flags disagree with the live gateway:\n" + "\n".join(bad), file=sys.stderr)
    print("Update PINNED_FLAGS in this script after deciding which value is right.", file=sys.stderr)
    sys.exit(1)
print("   all pinned flags match the gateway")
PY

ENV_VARS="PIVOTA_ENV=$PIVOTA_ENV,PIVOTA_SERVICE_NAME=$JOB,PIVOTA_COMMIT_SHA=$GATEWAY_TAG,DB_POOL_MAX=3"
while IFS= read -r line; do
  case "$line" in *"=<unset>") ;; *) ENV_VARS="$ENV_VARS,$line" ;; esac
done <<EOF
$PINNED_FLAGS
EOF

echo "== job: $JOB (default subnet, dry run unless an execution passes --write; no trigger)"
verb=create; have "$GCLOUD" run jobs describe "$JOB" --region "$REGION" && verb=update
# --max-retries 0: a promote that failed half-way is looked at before it is run again.
"$GCLOUD" run jobs "$verb" "$JOB" --region "$REGION" --image "$GATEWAY_IMAGE" --service-account "$SA" \
  --network default --subnet default --vpc-egress all-traffic \
  --max-retries 0 --task-timeout 1800s --cpu 1 --memory 2Gi \
  --labels "env=$ENV,managed-by=infra-gcp,lane=pdp-live-read-promote" \
  --set-secrets "DATABASE_URL=DATABASE_URL_NOVERIFY:latest" \
  --set-env-vars "$ENV_VARS" \
  --command node --args="scripts/promote-pdp-identity-live-read.js,--limit,500,--created-by,cloud-run-job" --quiet

echo "READY: $JOB exists and has no trigger. Its own args are a dry run; see the header for a write."
