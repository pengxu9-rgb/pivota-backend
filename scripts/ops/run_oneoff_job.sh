#!/usr/bin/env bash
# Run a one-off script inside the PRODUCTION environment (Cloud Run, pivota-prod/us-west1).
#
# This is the GCP replacement for `railway run` / `railway ssh`, which have no equivalent here:
# Cloud Run gives you no way to attach to a running instance. The pattern is a throwaway Cloud Run
# JOB on the production image, exactly as documented in docs/runbooks/operating_on_gcp_production.md
# and as already used by probe_health() in infra/gcp/deploy_backend.sh.
#
# It exists as a script rather than as a block copied into twenty docstrings because the pattern has
# three footguns that are easy to get wrong once and then wrong everywhere:
#
#   1. A JOB INHERITS NOTHING. Not one env var, not one secret. A script that reads DATABASE_URL
#      gets nothing unless it is mounted, and fails in a way that reads as a database outage rather
#      than a missing mount. Default below is the bare `DATABASE_URL` secret (most of this project's
#      secrets carry an `env-` prefix; DATABASE_URL, REDIS_URL and PCI_KB_DATABASE_URL do not).
#   2. `--args` SPLITS ON COMMAS. Any argument containing a comma is silently shredded into separate
#      argv entries. We use gcloud's alternate-delimiter form and CHOOSE a delimiter that does not
#      occur in the payload, rather than hardcoding one that eventually will.
#   3. A LEFT-BEHIND JOB is a standing execution surface with a service account attached. Deleted on
#      every exit path, including Ctrl-C.
#
# THE VERDICT IS THE JOB'S EXIT CODE, NEVER THE LOG. Cloud Logging ingestion lag is unbounded, so a
# read taken right after the run can come back empty, which looks exactly like "it printed nothing"
# or "it never ran". Believing a log read over an exit code stranded a healthy revision at 0%
# traffic on 2026-08-25. `--wait` already carries the answer out; the log is fetched for DETAIL
# only, behind a retry, and its emptiness is reported as such instead of being read as a result.
#
# Usage:
#   scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py
#   scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py --billing-run-id 28 --json
#   SECRETS=DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest \
#     scripts/ops/run_oneoff_job.sh scripts/some_script.py --apply
#
# Environment overrides: PROJECT, REGION, IMAGE, SERVICE_ACCOUNT, SECRETS, TASK_TIMEOUT, JOB_PREFIX.
# Exits with the job's own exit status, so `... && echo ok` and `set -e` behave as you expect.
set -euo pipefail

PROJECT="${PROJECT:-pivota-prod}"
REGION="${REGION:-us-west1}"
IMAGE="${IMAGE:-us-west1-docker.pkg.dev/pivota-shared/pivota/backend:latest}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-sa-worker@$PROJECT.iam.gserviceaccount.com}"
SECRETS="${SECRETS:-DATABASE_URL=DATABASE_URL:latest}"
TASK_TIMEOUT="${TASK_TIMEOUT:-600s}"
JOB_PREFIX="${JOB_PREFIX:-oneoff}"

[ "$#" -ge 1 ] || { sed -n '1,40p' "$0" >&2; exit 2; }

# Pick a delimiter that cannot appear in the payload. gcloud's alternate-delimiter form is
# `--args="^X^a^Xb"` -> literally `^X^` then values joined by X; if X occurs inside a value the
# split is silent and wrong, so this is checked rather than assumed.
DELIM=""
for c in '|' '@' '#' '%' '~' '+' '!' ';' ':' '?'; do
  if ! printf '%s\0' "$@" | grep -qF -- "$c"; then DELIM="$c"; break; fi
done
[ -n "$DELIM" ] || { echo "run_oneoff_job: no safe --args delimiter for these arguments; pass fewer/simpler args" >&2; exit 2; }

JOINED=""
for a in "$@"; do JOINED="${JOINED:+$JOINED$DELIM}$a"; done

JOB="$JOB_PREFIX-$$-$RANDOM"
cleanup(){ gcloud run jobs delete "$JOB" --project "$PROJECT" --region "$REGION" --quiet >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

echo "==> job $JOB  image ${IMAGE##*/}  secrets ${SECRETS%%=*}..." >&2
gcloud run jobs create "$JOB" --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$SERVICE_ACCOUNT" \
  --network default --subnet default --vpc-egress all-traffic \
  --set-secrets "$SECRETS" \
  --max-retries 0 --task-timeout "$TASK_TIMEOUT" \
  --command python --args="^$DELIM^$JOINED" \
  --quiet >/dev/null

RC=0
gcloud run jobs execute "$JOB" --project "$PROJECT" --region "$REGION" --wait --quiet >/dev/null 2>&1 || RC=$?

# Detail only. Six tries at 5s: enough for the usual lag, and its failure is reported as a failure
# to READ rather than as an empty result.
OUT=""
for _ in 1 2 3 4 5 6; do
  OUT=$(gcloud logging read "resource.labels.job_name=\"$JOB\"" --project "$PROJECT" \
    --limit 1000 --format='value(textPayload)' --freshness=10m 2>/dev/null) || OUT=""
  [ -n "$OUT" ] && break
  sleep 5
done

if [ -n "$OUT" ]; then
  # Cloud Logging returns newest-first; replay in the order the script actually printed.
  printf '%s\n' "$OUT" | awk '{L[NR]=$0} END {for(i=NR;i>=1;i--) print L[i]}'
else
  echo "(no log entries readable after 30s - ingestion lag, not a verdict; the exit code below is the verdict)" >&2
fi

if [ "$RC" = 0 ]; then echo "==> job succeeded (exit 0)" >&2; else echo "==> job FAILED (exit $RC) - the exit code is the verdict, do not re-read the log for one" >&2; fi
exit "$RC"
