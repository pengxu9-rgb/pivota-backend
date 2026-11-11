#!/usr/bin/env bash
# Provision the brand-official canonical promote lane: ONE Cloud Run Job
# (`brand-official-canonical-promote`, backend image) and its daily Cloud Scheduler trigger.
#
#   infra/gcp/setup_brand_official_canonical_promote_job.sh staging|prod <backend-tag>                    # dark
#   infra/gcp/setup_brand_official_canonical_promote_job.sh staging|prod <backend-tag> --enable           # armed, dry run
#   infra/gcp/setup_brand_official_canonical_promote_job.sh staging|prod <backend-tag> --enable --apply   # armed, writes
#
# FOR THE OPERATOR. Not run by CI or by any deploy script. Runbook: docs/runbooks/catalog_jobs_on_gcp.md.
#
# WHAT IT RUNS. scripts/promote_brand_official_canonicals.py: for enrichment-minted brand-official
# canonicals (source_system='catalog_enrichment_agent_v1', live) that do not yet hold a
# serving-eligible index_pipeline_state row, it writes the quality snapshot, recomputes serving
# eligibility and upserts catalog_row_trust — the "step 2" that had to follow every curated ingest
# and that nothing scheduled did, so it was run by hand through scripts/ops/run_oneoff_job.sh.
# It never fabricates content: rows that fail the classifier stay blocked and are counted.
# Its population EXCLUDES rows that are already serving-eligible, so a run cannot re-promote or
# rewrite them, and a run with nothing new to promote does nothing.
#
# THREE STATES, chosen on THIS run (re-running with fewer flags steps the lane back down):
#   no flags          trigger PAUSED, job args are a DRY RUN. A hand execution writes nothing.
#   --enable          trigger RESUMED, job args are a DRY RUN: a daily would-be census in the log.
#   --enable --apply  trigger RESUMED, job args carry --apply: it writes. --apply alone is refused.
# A run that does not WRITE (no flags, or --enable alone) pauses the trigger FIRST, before any
# preflight, so worker drift or a missing image can never keep an --apply lane running; an --enable
# run resumes it at the end, after the job's args have been stepped back to a dry run. Fastest disarm, no script needed:
#   gcloud scheduler jobs pause brand-official-canonical-promote-cron --location us-west1 --project pivota-prod
#
# THIS SCRIPT OWNS THE JOB'S WHOLE ENV AND SECRETS: --set-env-vars / --set-secrets REPLACE the set on
# every run, so a value added by hand is wiped by the next run. Pin it here instead.
#
# NO --limit, ON PURPOSE. The population is "not yet serving-eligible", and a row the classifier
# blocks STAYS in it, so a daily --limit would re-score the same first N blocked rows forever and
# never reach newer ones. Size is checked instead: arm with --enable first and read the dry-run
# census (rows scored, would-be pass/fail per blocker) before adding --apply.
#
# PINNED ENV, VERIFIED AGAINST THE WORKER. A Cloud Run Job inherits nothing. Run through the one-off
# runner, this script computed eligibility WITHOUT ENABLE_KBEAUTY_AGENT_DECISION_GATES and promoted
# 410 rows the gates would have blocked (2026-09-08), so every catalog flag that `worker` carries and
# that this path reads is pinned below, and the preflight REFUSES to provision if the live worker's
# value differs: a job whose gates disagree with the service that re-gates its rows every night is
# the defect, so drift stops this script instead of being copied silently. Update the pin here.
# `<unset>` means the worker does not set the flag and neither may the job: these are flags the
# write path reads (serving eligibility, the trust upsert, the PDP view heal) that are off today, so
# turning one on in `worker` stops this script until the pin follows.
#
# SCHEDULE: 08:25 UTC daily, default subnet (Cloud SQL only; no merchant fetches, so not the crawl
# address). Clear of nightly_index_health (04:00), catalog_invariant_sweep (04:50), the 07:15 and
# 09:15 external-referral-refresh runs, and the 10:15-11:45 window reserved for relgraph-sync.
# INDEXNOW_ENABLED=true is pinned like the worker's, so a promoted URL may be pinged to IndexNow
# from the default NAT, exactly as the worker's own promotions are.
set -euo pipefail

ENV="${1:-}"; BACKEND_TAG="${2:-}"; shift 2 2>/dev/null || true
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <backend-tag> [--enable [--apply]]" >&2; exit 2 ;;
esac
[ -n "$BACKEND_TAG" ] || { echo "backend-tag is required" >&2; exit 2; }
case "$BACKEND_TAG" in
  latest) echo "refusing the floating tag 'latest' - pass the commit sha" >&2; exit 2 ;;
esac
ENABLED=false; APPLY=false
for arg in "$@"; do
  case "$arg" in
    --enable) ENABLED=true ;;
    --apply) APPLY=true ;;
    *) echo "unknown argument '$arg' (options: --enable, --apply)" >&2; exit 2 ;;
  esac
done
[ "$#" -le 2 ] || { echo "too many arguments" >&2; exit 2; }
if [ "$APPLY" = true ] && [ "$ENABLED" != true ]; then
  echo "--apply needs --enable: a dark lane that writes on its next hand execution is not dark" >&2; exit 2
fi

GCLOUD="${GCLOUD:-gcloud}"; PYTHON="${PYTHON:-python3}"; REGION=us-west1; SHARED=pivota-shared
JOB=brand-official-canonical-promote
TRIGGER=brand-official-canonical-promote-cron
SCHEDULE="25 8 * * *"
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
BACKEND_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/backend:$BACKEND_TAG"
have(){ "$@" >/dev/null 2>&1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"

# The catalog flags this path reads that `worker` sets. Name=value, one per line.
PINNED_FLAGS="AGENT_PDP_VIEW_MARKET_PRICES=on
ENABLE_KBEAUTY_AGENT_DECISION_GATES=true
ENABLE_STORELESS_BRAND_CATALOG=1
INDEX_ELIGIBLE_READ=1
INDEX_ELIGIBLE_RECALL=1
INDEX_ELIGIBLE_SITEMAP=1
INDEXNOW_ENABLED=true
PDP_QUALITY_SCORE_SOURCE_BACKED_OPTIONAL_COMPONENTS=1
PIVOTA_SERVING_PRICING_REGIONS=US,SG
SERVE_PDP_ENRICHMENT_ON_WRITE=1
SERVING_PRICE_REQUIRES_SEED_CURRENCY=1
STRICT_BEAUTY_CATEGORY_TEXT_RECALL=true
CATEGORY_LEAF_REQUIRED_FOR_SERVING=<unset>
CATALOG_TRUST_CRAWL_FRESH_ENABLED=<unset>
CATALOG_TRUST_CRAWL_FRESH_DAYS=<unset>
CATALOG_TRUST_CRAWL_FRESH_RECHECK_LIMIT=<unset>
AGENT_PDP_VIEW_UNBUILDABLE_REAP=<unset>
ENABLE_KBEAUTY_EVIDENCE_GATES=<unset>
CATALOG_TRUST_RENDERABLE_GATE=<unset>"

# THE FAMILY GUARD (delta review, D-P2-2): a grep of today's code always lags tomorrow's. So beyond
# the pins, the preflight REFUSES when the worker sets ANY plain-value flag in these catalog families
# that is neither pinned above nor listed below as read only elsewhere. A new eligibility or trust
# flag turned on in `worker` stops this script until someone decides how the job should carry it.
CATALOG_FLAG_FAMILIES="AGENT_PDP_VIEW_ CATALOG_TRUST_ CATEGORY_ ENABLE_KBEAUTY_ ENABLE_STORELESS_ INDEX_ INDEXNOW_ PDP_QUALITY_ PIVOTA_SERVING_ SERVE_PDP_ SERVING_ STRICT_BEAUTY_"
# Set on the worker, in a family above, and NOT read by this path. Each needs its reason.
#   AGENT_PDP_VIEW_RECONCILE_LIMIT   batch size of jobs/agent_pdp_view_reconciler_cron.py only
NOT_READ_BY_THIS_PATH="AGENT_PDP_VIEW_RECONCILE_LIMIT"

# DISARM FIRST: any run that does not WRITE (no flags, or --enable alone) pauses the trigger before
# anything below can refuse, so a preflight refusal can never leave an --apply lane running. The pause
# is always attempted; only "not found" (no trigger yet) is fine. An --enable run resumes it at the end.
if [ "$APPLY" != true ]; then
  _err="$("$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet 2>&1 >/dev/null)" && \
    echo "== paused $TRIGGER (disarm runs before the preflight)" || {
    case "$_err" in
      *NOT_FOUND*|*"not found"*|*"Not found"*) ;;
      *) printf 'FAILED to pause %s - it may be LIVE:\n%s\n' "$TRIGGER" "$_err" >&2; exit 1 ;;
    esac
  }
fi

have "$GCLOUD" iam service-accounts describe "$SA" || { echo "missing $SA" >&2; exit 1; }
have "$GCLOUD" artifacts docker images describe "$BACKEND_IMAGE" \
  || { echo "missing image $BACKEND_IMAGE; build it first" >&2; exit 1; }

echo "== preflight: pinned flags vs the live worker service"
WORKER_JSON="$("$GCLOUD" run services describe worker --region "$REGION" --format=json)" \
  || { echo "cannot read the worker service; refusing to provision without verifying the pinned flags" >&2; exit 1; }
WORKER_JSON="$WORKER_JSON" PINNED_FLAGS="$PINNED_FLAGS" FAMILIES="$CATALOG_FLAG_FAMILIES" \
  NOT_READ="$NOT_READ_BY_THIS_PATH" "$PYTHON" - <<'PY' || exit 1
import json, os, sys
doc = json.loads(os.environ["WORKER_JSON"])
env = {}
for e in doc["spec"]["template"]["spec"]["containers"][0].get("env", []):
    env[e["name"]] = e.get("value") if "value" in e else None
bad = []
pinned = set()
for line in os.environ["PINNED_FLAGS"].splitlines():
    name, want = line.split("=", 1)
    pinned.add(name)
    have = env.get(name, "<unset>")
    if have != want:
        bad.append(f"  {name}: pinned {want!r}, worker has {have!r}")
families = tuple(os.environ["FAMILIES"].split())
not_read = set(os.environ["NOT_READ"].split())
for name in sorted(env):
    if name.startswith(families) and name not in pinned and name not in not_read:
        bad.append(f"  {name}: set on the worker, in a catalog flag family, and neither pinned nor listed as not read")
if bad:
    print("REFUSING: pinned flags disagree with the live worker:\n" + "\n".join(bad), file=sys.stderr)
    print("Update PINNED_FLAGS in this script after deciding which value is right.", file=sys.stderr)
    sys.exit(1)
print("   all pinned flags match the worker")
PY

# `^|^`: PIVOTA_SERVING_PRICING_REGIONS holds a comma, so this list is `|`-delimited.
ENV_VARS="^|^PIVOTA_ENV=$PIVOTA_ENV|PIVOTA_SERVICE_NAME=$JOB|PIVOTA_COMMIT_SHA=$BACKEND_TAG"
ENV_VARS="$ENV_VARS|DB_POOL_MIN_SIZE=1|DB_POOL_MAX_SIZE=2"
ENV_VARS="$ENV_VARS|DB_STATEMENT_TIMEOUT_SECONDS=30|DB_COMMAND_TIMEOUT_SECONDS=600"
while IFS= read -r line; do
  case "$line" in *"=<unset>") ;; *) ENV_VARS="$ENV_VARS|$line" ;; esac
done <<EOF
$PINNED_FLAGS
EOF

ARGS="scripts/promote_brand_official_canonicals.py"
[ "$APPLY" = true ] && ARGS="$ARGS,--apply"

echo "== job: $JOB (default subnet, $( [ "$APPLY" = true ] && echo WRITES || echo dry run ))"
verb=create; have "$GCLOUD" run jobs describe "$JOB" --region "$REGION" && verb=update
# --max-retries 0: a failed run is looked at, not repeated; the next day's run is the retry.
"$GCLOUD" run jobs "$verb" "$JOB" --region "$REGION" --image "$BACKEND_IMAGE" --service-account "$SA" \
  --network default --subnet default --vpc-egress all-traffic \
  --max-retries 0 --task-timeout 3600s --cpu 1 --memory 2Gi \
  --labels "env=$ENV,managed-by=infra-gcp,lane=brand-official-promote" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "$ENV_VARS" \
  --command python --args="$ARGS" --quiet

echo "== trigger: $TRIGGER ($SCHEDULE UTC)"
URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run"
verb=create; have "$GCLOUD" scheduler jobs describe "$TRIGGER" --location "$REGION" && verb=update
"$GCLOUD" scheduler jobs "$verb" http "$TRIGGER" --location "$REGION" \
  --schedule="$SCHEDULE" --time-zone=Etc/UTC --uri="$URI" --http-method=POST \
  --oauth-service-account-email="$SA" --attempt-deadline=300s --quiet

if [ "$ENABLED" = true ]; then
  "$GCLOUD" scheduler jobs resume "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to resume $TRIGGER" >&2; exit 1; }
  echo "ARMED: $JOB runs daily at 08:25 UTC ($( [ "$APPLY" = true ] && echo 'writes' || echo 'dry run; add --apply to write' ))."
else
  "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to pause $TRIGGER - it may be LIVE" >&2; exit 1; }
  echo "DARK: $TRIGGER is paused and the job's args are a dry run. Arm with --enable."
fi
