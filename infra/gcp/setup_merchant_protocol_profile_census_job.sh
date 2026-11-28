#!/usr/bin/env bash
# Provision the merchant protocol census: ONE Cloud Run Job on the crawl subnet and its Cloud
# Scheduler trigger, every two weeks. Nothing else: no worker, no other job, no other trigger.
#
#   infra/gcp/setup_merchant_protocol_profile_census_job.sh staging|prod <backend-sha>            # dark
#   infra/gcp/setup_merchant_protocol_profile_census_job.sh staging|prod <backend-sha> --enable   # armed
#
# FOR THE OPERATOR. Not run by CI, not run by deploy_backend.sh, and a backend deploy never
# re-images this job: it runs <backend-sha> until this script (or reimage_jobs.sh --apply naming
# it) points it at a newer one. Runbook: docs/runbooks/merchant_agent_checkout_facts.md.
#
# WHY A SCHEDULE. The census (jobs/merchant_protocol_profile_census.py --from-directory --store)
# writes merchant_agent_checkout_facts, the stored fact ADR-030 D11 reads. A positive counts only
# while it is younger than MERCHANT_AGENT_CHECKOUT_FACT_MAX_AGE_DAYS (30). With
# PARTNER_AGENT_RUN_CONDITION_RAILS on, every merchant whose positive has aged out is refused. Until
# this lane, every run was a laptop one-off with its own go; one missed rerun would age out every
# merchant at once.
#
# THE RUN is the reviewed first run, unchanged (2026-11-28, 198 domains written): the same module,
# args, env, egress and database guardrails. Only who starts it changes.
#
# BACKEND SHA ONLY. The image is backend:<40-hex commit>. `latest`, a short sha or any other tag is
# refused: a job runs the image it was last given for weeks, so the tag must name one commit.
#
# DARK BY DEFAULT, ARMED ONLY BY --enable. One flag moves both switches together:
#   without --enable: MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED=false on the job, trigger PAUSED
#   with    --enable: MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED=true  on the job, trigger ENABLED
# Re-running WITHOUT --enable disarms an armed lane, deliberately. The gate is also read INSIDE the
# job, so an execution started by hand against a dark job prints UCPPROF_DISABLED, exits 0 and
# contacts nobody.
#
# THE TWO SWITCHES NEVER DISAGREE SILENTLY. A dark job exits 0, and the Cloud Run success metric the
# missed-run alert reads counts that as a success, so "gate false, trigger enabled" would hide a
# lapse; "gate true, trigger paused" is a live job nobody schedules. So:
#   * disarming pauses an existing trigger BEFORE the job is touched;
#   * a NEW trigger is paused at once (`scheduler jobs create` has no --paused; it starts ENABLED);
#   * arming resumes the trigger only after the job carries gate=true, and a failed resume puts the
#     gate back to false;
#   * the script then READS BACK both switches; on any exit, success or failure, a pair that
#     disagrees is printed as `MISMATCH` with a non-zero exit, and an arming run that ends in a
#     mismatch tries once to put both to dark first.
# tests/test_setup_merchant_protocol_profile_census_job.py fails each step in turn and checks it.
#
# --max-retries 0: a failed execution is not re-run automatically. A retry would ask every merchant
# again; the alerts page and a person decides on a hand execution (the runbook's one-off rules).
#
# SCHEDULE: 11:47 UTC on the 1st and 15th of every month (gaps of 14-17 days; unix cron cannot count
# 14 days, and a day-of-month plus day-of-week rule is an OR). The latest end is 11:47 + the task
# timeout = 12:00. Every neighbour on the crawl address, each as its LONGEST window (task timeout x
# (max-retries + 1)), and the prod one-off house windows, are clear of 11:47-12:00:
#   * merchant-purchasability-sweep   hourly :07, done by :27; it holds the Shopify edge pacer;
#   * the Reap proof enrichment job  06:41, task-timeout 10800s -> 09:41; pacer holder;
#   * the Reap proof mirror job      13:13, task-timeout 15000s -> 17:23; pacer holder. THIS is why
#                                     the slot is not 15:47: that window overlaps the mirror's;
#   * external-seed-destination-sweep 02:20, tierb-cart-link-eligibility 03:30,
#     catalog-destination-sweep 04:10: all done by 06:10;
#   * house windows: hourly :08-:26 and :37-:46, the referral refresh HH:15-(HH+1):10 in hours
#     1,5,7,9,12,14,16,18,20,22 (12:15 is after 12:00), daily 03:00-04:30, 05:15-06:15, 10:30-11:20.
# The census takes no lease on the shared Shopify edge pacer (CRAWL_SHOPIFY_EDGE_* are not set: it
# paces itself, one request a second across the whole run), so the test also re-derives that no
# pacer holder's window overlaps it. Two scheduled executions can never overlap (780 s against 14
# days); a HAND execution can overlap a scheduled one: see the runbook.
set -euo pipefail

ENV="${1:-}"; BACKEND_TAG="${2:-}"; FLAG_ARG="${3:-}"
case "$ENV" in
  prod) PROJECT=pivota-prod; PIVOTA_ENV=production ;;
  staging) PROJECT=pivota-staging; PIVOTA_ENV=staging ;;
  *) echo "usage: $0 staging|prod <backend-sha> [--enable]" >&2; exit 2 ;;
esac
[ -n "$BACKEND_TAG" ] || { echo "backend-sha is required" >&2; exit 2; }
[[ "$BACKEND_TAG" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "backend-sha must be a full 40-hex commit sha (got '$BACKEND_TAG'); 'latest' and short shas are refused" >&2; exit 2; }
case "$FLAG_ARG" in
  "") ENABLED=false ;;
  --enable) ENABLED=true ;;
  *) echo "unknown argument '$FLAG_ARG' (the only option is --enable)" >&2; exit 2 ;;
esac
[ "$#" -le 3 ] || { echo "too many arguments" >&2; exit 2; }

GCLOUD="${GCLOUD:-gcloud}"; PYTHON="${PYTHON:-python3}"; REGION=us-west1; SHARED=pivota-shared
JOB=merchant-protocol-profile-census
TRIGGER=merchant-protocol-profile-census-cron
SCHEDULE="47 11 1,15 * *"
SUBNET=pivota-crawl
GATE=MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED
# The crawl deadline and the task timeout, as the reviewed first run: 690 s of crawl, then 90 s for
# the container start, the directory read and the last writes.
DEADLINE_SECONDS=690
TASK_TIMEOUT=780s
# The worker identity, exactly as the purchasability sweep and the Tier B job use it: it already
# holds Secret Manager access to DATABASE_URL and project-level run.invoker for Scheduler.
SA="sa-worker@$PROJECT.iam.gserviceaccount.com"
BACKEND_IMAGE="$REGION-docker.pkg.dev/$SHARED/pivota/backend:$BACKEND_TAG"
URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run"
have(){ "$@" >/dev/null 2>&1; }
export CLOUDSDK_CORE_PROJECT="$PROJECT"

have "$GCLOUD" iam service-accounts describe "$SA" || { echo "missing $SA" >&2; exit 1; }
have "$GCLOUD" artifacts docker images describe "$BACKEND_IMAGE" \
  || { echo "missing image $BACKEND_IMAGE; build it first" >&2; exit 1; }
# The crawl subnet must exist, or --subnet would fail late; and it must be the crawl NAT's.
have "$GCLOUD" compute networks subnets describe "$SUBNET" --region "$REGION" \
  || { echo "missing subnet $SUBNET; run setup_crawl_egress.sh first" >&2; exit 1; }

# The first run's env, exactly. PIVOTA_ENV is required (a Job inherits nothing); the DB timeouts
# re-supply the guardrails `web` has (db/database.py defaults both to OFF); one pooled connection.
ENV_VARS="PIVOTA_ENV=$PIVOTA_ENV|PIVOTA_SERVICE_NAME=$JOB|PIVOTA_COMMIT_SHA=$BACKEND_TAG"
ENV_VARS="$ENV_VARS|DB_AUTO_MIGRATE=false|DB_POOL_MIN_SIZE=1|DB_POOL_MAX_SIZE=1"
ENV_VARS="$ENV_VARS|DB_STATEMENT_TIMEOUT_SECONDS=30|DB_COMMAND_TIMEOUT_SECONDS=600"
ENV_VARS="$ENV_VARS|$GATE=$ENABLED"
ARGS="-m,jobs.merchant_protocol_profile_census,--from-directory,--store,--deadline-seconds,$DEADLINE_SECONDS"

# The two switches as they stand. gate: true | false | absent (no job). trigger: ENABLED | PAUSED |
# absent | <other state>. A read that fails is `unknown`, never a guess.
job_gate(){
  local spec
  have "$GCLOUD" run jobs describe "$JOB" --region "$REGION" || { echo absent; return; }
  spec=$("$GCLOUD" run jobs describe "$JOB" --region "$REGION" --format=json 2>/dev/null) || { echo unknown; return; }
  printf '%s' "$spec" | "$PYTHON" -c '
import json, sys
try:
    containers = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"]
    env = {e.get("name"): e.get("value") for e in containers[0].get("env", [])}
except Exception:
    print("unknown"); sys.exit(0)
value = str(env.get(sys.argv[1]) or "").strip().lower()
print("true" if value in ("1", "true", "on", "yes") else "false")' "$GATE" 2>/dev/null || echo unknown
}
trigger_state(){
  have "$GCLOUD" scheduler jobs describe "$TRIGGER" --location "$REGION" || { echo absent; return; }
  "$GCLOUD" scheduler jobs describe "$TRIGGER" --location "$REGION" --format='value(state)' 2>/dev/null || echo unknown
}
# Agree: gate true with the trigger ENABLED, or gate false (or no job) with the trigger PAUSED or absent.
pair_agrees(){ # gate trigger
  case "$1:$2" in
    true:ENABLED|false:PAUSED|false:absent|absent:PAUSED|absent:absent) return 0 ;;
    *) return 1 ;;
  esac
}

on_exit(){
  local rc=$? gate trig
  trap - EXIT
  gate=$(job_gate); trig=$(trigger_state)
  if ! pair_agrees "$gate" "$trig" && [ "$ENABLED" = true ]; then
    # An arming run that cannot finish goes dark rather than half-armed.
    "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet >/dev/null 2>&1 || true
    "$GCLOUD" run jobs update "$JOB" --region "$REGION" --update-env-vars "$GATE=false" --quiet >/dev/null 2>&1 || true
    gate=$(job_gate); trig=$(trigger_state)
  fi
  if ! pair_agrees "$gate" "$trig"; then
    echo "MISMATCH: $JOB gate=$gate, $TRIGGER state=$trig. The two switches disagree; re-run this script (without --enable to go dark) before anything else." >&2
    [ "$rc" -ne 0 ] || rc=1
  fi
  exit "$rc"
}
trap on_exit EXIT

if [ "$ENABLED" = false ] && [ "$(trigger_state)" != absent ]; then
  # Stop the firing first: an existing trigger is paused before the job is touched.
  "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to pause $TRIGGER - it may be LIVE; nothing else was changed" >&2; exit 1; }
fi

echo "== job: $JOB (subnet $SUBNET, gate $ENABLED, image $BACKEND_TAG)"
verb=create; have "$GCLOUD" run jobs describe "$JOB" --region "$REGION" && verb=update
# --args= in the EQUALS form: the value starts with a dash, and `--args "-m,..."` is parsed by gcloud
# as a second flag ("argument --args: expected one argument"), #2367.
"$GCLOUD" run jobs "$verb" "$JOB" --region "$REGION" --image "$BACKEND_IMAGE" --service-account "$SA" \
  --network default --subnet "$SUBNET" --vpc-egress all-traffic \
  --max-retries 0 --task-timeout "$TASK_TIMEOUT" --cpu 1 --memory 1Gi \
  --labels "env=$ENV,managed-by=infra-gcp,lane=merchant-protocol-census" \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
  --set-env-vars "^|^$ENV_VARS" \
  --command python --args="$ARGS" --quiet

echo "== trigger: $TRIGGER ($SCHEDULE UTC)"
verb=create; have "$GCLOUD" scheduler jobs describe "$TRIGGER" --location "$REGION" && verb=update
"$GCLOUD" scheduler jobs "$verb" http "$TRIGGER" --location "$REGION" \
  --schedule="$SCHEDULE" --time-zone=Etc/UTC --uri="$URI" --http-method=POST \
  --oauth-service-account-email="$SA" --attempt-deadline=300s --quiet
if [ "$verb" = create ]; then
  # A new trigger starts ENABLED: pause it before anything else, so it is never live because it was
  # just created.
  "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to pause the new $TRIGGER - it may be LIVE" >&2; exit 1; }
fi

if [ "$ENABLED" = true ]; then
  if ! "$GCLOUD" scheduler jobs resume "$TRIGGER" --location "$REGION" --quiet; then
    echo "FAILED to resume $TRIGGER; putting the gate back to false" >&2
    "$GCLOUD" run jobs update "$JOB" --region "$REGION" --update-env-vars "$GATE=false" --quiet \
      || echo "FAILED to put the gate back to false" >&2
    exit 1
  fi
else
  "$GCLOUD" scheduler jobs pause "$TRIGGER" --location "$REGION" --quiet \
    || { echo "FAILED to pause $TRIGGER - it may be LIVE" >&2; exit 1; }
fi

GATE_NOW=$(job_gate); TRIGGER_NOW=$(trigger_state)
case "$ENABLED:$GATE_NOW:$TRIGGER_NOW" in
  true:true:ENABLED) echo "ARMED: $JOB runs at 11:47 UTC on the 1st and 15th, gate on, image $BACKEND_TAG." ;;
  false:false:PAUSED) echo "DARK: $TRIGGER is paused and the job's gate is false. Arm with --enable." ;;
  *) echo "FAILED: read back gate=$GATE_NOW trigger=$TRIGGER_NOW, expected the $( [ "$ENABLED" = true ] && echo armed || echo dark ) pair" >&2; exit 1 ;;
esac
