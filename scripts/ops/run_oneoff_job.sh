#!/usr/bin/env bash
# Run a one-off script inside the PRODUCTION environment (Cloud Run, pivota-prod/us-west1).
#
# This is the GCP replacement for `railway run` / `railway ssh`, which have no equivalent here:
# Cloud Run gives you no way to attach to a running instance. The pattern is a throwaway Cloud Run
# JOB on the production image, exactly as documented in docs/runbooks/operating_on_gcp_production.md
# and as already used by probe_health() in infra/gcp/deploy_backend.sh.
#
# It exists as a script rather than as a block copied into twenty docstrings because the pattern has
# several footguns that are easy to get wrong once and then wrong everywhere:
#
#   1. A JOB INHERITS NOTHING. Not one env var, not one secret. A script that reads DATABASE_URL
#      gets nothing unless it is mounted, and fails in a way that reads as a database outage rather
#      than a missing mount. Secret NAMES are not uniform - see the runbook's list; read the
#      secretKeyRef off the running service rather than trusting any list, that one included.
#   2. INHERITING NOTHING ALSO DROPS THE GUARDRAILS. `web` sets DB_STATEMENT_TIMEOUT_SECONDS=30 and
#      DB_COMMAND_TIMEOUT_SECONDS=600, while db/database.py defaults BOTH to 0.0, which means OFF.
#      A job that did not re-supply them would run against the production database with statement
#      timeouts disabled - the inverse of the safe default, against this project's pool-wedge
#      history. ENV_VARS below re-supplies them; keep them unless you have decided otherwise.
#   3. `--args` SPLITS ON COMMAS, and gcloud's alternate-delimiter parser also DROPS a leading or
#      trailing empty value (`^|^a|b|` -> ['a','b']). We choose a delimiter absent from the payload
#      rather than hardcoding one that eventually appears, and refuse empty arguments outright
#      instead of shipping a silently shifted argv.
#   4. A LEFT-BEHIND JOB is a standing execution surface with a service account attached. Deleted on
#      every exit path, including Ctrl-C.
#
# THE EXIT CODE IS THE VERDICT, NOT THE LOG. Cloud Logging ingestion lag is unbounded, so a read
# taken right after the run can come back empty, which looks exactly like "it printed nothing" or
# "it never ran". Believing a log read over an exit code stranded a healthy revision at 0% traffic
# on 2026-08-25. `--wait` already carries pass/fail out; the log is fetched for DETAIL only, behind
# a retry, and its emptiness is reported as such instead of being read as a result.
#
# Precisely what this script's exit status means: 0 = the job's container exited 0. Non-zero = it
# did not, OR gcloud itself failed. It is NOT the container's own exit code - `gcloud run jobs
# execute` raises ExecutionFailedError, which carries no exit_code, so calliope exits 1 for every
# job failure alike. That is why gcloud's stderr is captured and printed on failure rather than
# discarded: the exit code says THAT it failed, and only that text says WHY.
#
# Usage:
#   scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py
#   scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py --billing-run-id 28 --json
#   SECRETS=DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest \
#     scripts/ops/run_oneoff_job.sh scripts/some_script.py --apply
#
# Environment overrides: PROJECT, REGION, IMAGE, SERVICE_ACCOUNT, SECRETS, ENV_VARS, TASK_TIMEOUT,
# JOB_PREFIX. SECRETS and ENV_VARS are passed to gcloud verbatim and are themselves comma-separated,
# so no name or value in them may contain a comma.
#
# NEVER use this for a script that writes to a database it should not: it mounts PRODUCTION
# credentials. Read the target script's own docstring first.
set -euo pipefail

PROJECT="${PROJECT:-pivota-prod}"
REGION="${REGION:-us-west1}"
IMAGE="${IMAGE:-us-west1-docker.pkg.dev/pivota-shared/pivota/backend:latest}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-sa-worker@$PROJECT.iam.gserviceaccount.com}"
SECRETS="${SECRETS:-DATABASE_URL=DATABASE_URL:latest}"
ENV_VARS="${ENV_VARS:-DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600}"
TASK_TIMEOUT="${TASK_TIMEOUT:-600s}"
JOB_PREFIX="${JOB_PREFIX:-oneoff}"

# Print the whole header comment, however long it grows. A hardcoded line range
# silently truncates the moment the header changes — which it already has once.
usage(){ awk 'NR<3{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; }

case "${1-}" in
  -h|--help|"") usage >&2; exit 2 ;;
esac

# An empty argument cannot be expressed: gcloud drops it at either end of the list and shifts argv
# for everything after it. Refusing is the only honest option — an unset shell variable reaching
# here would otherwise run a DIFFERENT command than the one written down.
n=0
for a in "$@"; do
  n=$((n + 1))
  [ -n "$a" ] || { echo "run_oneoff_job: argument #$n is empty; gcloud --args cannot carry an empty value (check an unset variable)" >&2; exit 2; }
done

# Pick a delimiter that cannot appear in the payload. gcloud's alternate-delimiter form is
# `--args="^X^aXb"` -> the literal prefix `^X^`, then the values joined by X (so that example is two
# args, `a` and `b`). If X occurs inside a value the split is silent and wrong, so the choice is
# checked against the actual payload rather than assumed.
DELIM=""
for c in '|' '@' '#' '%' '~' '+' '!' ';' ':' '?'; do
  if ! printf '%s\0' "$@" | grep -qF -- "$c"; then DELIM="$c"; break; fi
done
[ -n "$DELIM" ] || { echo "run_oneoff_job: no safe --args delimiter for these arguments; pass fewer/simpler args" >&2; exit 2; }

JOINED=""
first=1
for a in "$@"; do
  if [ "$first" = 1 ]; then JOINED="$a"; first=0; else JOINED="$JOINED$DELIM$a"; fi
done

JOB="$JOB_PREFIX-$$-$RANDOM"
ERRLOG="$(mktemp -t oneoff-job-err)"
cleanup(){ gcloud run jobs delete "$JOB" --project "$PROJECT" --region "$REGION" --quiet >/dev/null 2>&1 || true; rm -f "$ERRLOG"; }
# Bare `trap cleanup INT` would run the handler and then CONTINUE, swallowing the signal: the script
# would finish the log read and exit 0, leaving no trace that anyone asked it to stop.
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

echo "==> job $JOB  image ${IMAGE##*/}  secrets ${SECRETS%%=*}..." >&2
gcloud run jobs create "$JOB" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$SERVICE_ACCOUNT" \
  --network default --subnet default --vpc-egress all-traffic \
  --set-secrets "$SECRETS" \
  --set-env-vars "$ENV_VARS" \
  --max-retries 0 --task-timeout "$TASK_TIMEOUT" \
  --command python --args="^$DELIM^$JOINED" \
  --quiet >/dev/null 2>"$ERRLOG" || {
    echo "==> could not CREATE the job (gcloud exited non-zero); it never ran:" >&2
    cat "$ERRLOG" >&2
    exit 2
  }

RC=0
gcloud run jobs execute "$JOB" --project "$PROJECT" --region "$REGION" --wait --quiet >/dev/null 2>"$ERRLOG" || RC=$?

# Detail only. Six tries at 5s: enough for the usual lag, and its failure is reported as a failure
# to READ rather than as an empty result.
#
# The logName filter is load-bearing. Without it the read also returns cloudaudit system_event
# entries, which carry no textPayload and render as blank lines interleaved through the output.
# jsonPayload.message is included because a structured logger writes there instead.
FRESHNESS="${FRESHNESS:-$(( ${TASK_TIMEOUT%s} + 600 ))s}"
FILTER="resource.labels.job_name=\"$JOB\" AND logName=~\"run.googleapis.com%2F(stdout|stderr)\$\""
OUT=""
for _ in 1 2 3 4 5 6; do
  OUT=$(gcloud logging read "$FILTER" --project "$PROJECT" \
    --limit 5000 --format='value(textPayload,jsonPayload.message)' --freshness="$FRESHNESS" 2>/dev/null) || OUT=""
  [ -n "$OUT" ] && break
  sleep 5
done

if [ -n "$OUT" ]; then
  # Cloud Logging returns newest-first; replay in the order the script actually printed. The sub()
  # strips the trailing tab that value() emits for the absent half of the payload pair.
  printf '%s\n' "$OUT" | awk '{sub(/\t+$/,""); L[NR]=$0} END {for(i=NR;i>=1;i--) print L[i]}'
else
  echo "(no log entries readable after 30s - ingestion lag, not a verdict; the exit code below is the verdict)" >&2
fi

if [ "$RC" = 0 ]; then
  echo "==> job succeeded (container exited 0)" >&2
else
  echo "==> job FAILED - the exit code is the verdict, do not re-read the log for one. gcloud said:" >&2
  cat "$ERRLOG" >&2
fi
exit "$RC"
