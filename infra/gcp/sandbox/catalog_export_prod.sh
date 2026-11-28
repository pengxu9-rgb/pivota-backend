#!/usr/bin/env bash
# RUNBOOK 10c-prod, the catalog export's role design R5: ONE catalog export as a PROD one-off
# (program and image reviewed by the PR controller, the owner's go in its session).
#
#   infra/gcp/sandbox/catalog_export_prod.sh <backend image by digest> <secret version number>            # dry run
#   APPLY=1 infra/gcp/sandbox/catalog_export_prod.sh <backend image by digest> <secret version number>    # the gated run
#
# Before it: R3 (`python -m jobs.sandbox_catalog_export_role grant`, as the tables' owner) and R4 (the
# owner sets the role's password and LOGIN, and adds the DSN as a NEW version of the secret; its NUMBER
# is the second argument, never `latest`). After it, always: R6 (NOLOGIN and end its sessions in psql,
# then `python -m jobs.sandbox_catalog_export_role revoke`, then disable that secret version).
#
# The export SA may read the role's secret ONLY while this script runs: the grant is added first and
# removed by an EXIT trap on every path (success, a failed deploy or execution, set -e, ^C), and the
# job is deleted there too, so no execution surface with the SA attached is left behind. The exit
# status is the run's, unless a cleanup fails: then the script ALWAYS exits non-zero (3 when the run
# itself succeeded) with a loud line, because a grant left on a prod secret is a failed step.
set -euo pipefail
# shellcheck source=infra/gcp/sandbox/config.sh
. "$(cd "$(dirname "$0")" && pwd)/config.sh"
PROD=pivota-prod
SECRET=CATALOG_EXPORT_DATABASE_URL
JOB=sandbox-catalog-export
IMG="${1:-}"
VERSION="${2:-}"
[[ "$IMG" =~ ^us-west1-docker\.pkg\.dev/pivota-shared/pivota/backend@sha256:[0-9a-f]{64}$ ]] \
  || { echo "usage: $0 us-west1-docker.pkg.dev/pivota-shared/pivota/backend@sha256:<digest> <secret version number>" >&2; exit 2; }
[[ "$VERSION" =~ ^[1-9][0-9]{0,5}$ ]] \
  || { echo "the secret version must be the NUMBER R4 printed (never latest); got '$VERSION'" >&2; exit 2; }
say "catalog export in $PROD -> gs://$DROP_BUCKET/catalog (secret $SECRET version $VERSION)"
if [ "$APPLY" = 1 ]; then   # every call below goes to pivota-prod, not the sandbox project
  echo "APPLY=1: changes WILL be made in project $PROD"
else
  echo "dry run (APPLY=0): nothing will be changed in $PROD; mutating commands are printed only"
fi

# This job's executions, by name. A dry run deployed nothing, so it prints the listing instead of
# reading prod (the dry-run guard covers this read too).
job_executions(){
  if [ "$APPLY" = 1 ]; then
    "$GCLOUD" run jobs executions list --job "$JOB" --project "$PROD" --region "$REGION" \
      --format='value(metadata.name)' --quiet 2>/dev/null
  else
    run "$GCLOUD" run jobs executions list --job "$JOB" --project "$PROD" --region "$REGION" \
      --format='value(metadata.name)' --quiet
  fi
}
cleanup(){
  local rc=$1 failed=0 name
  # The grant goes FIRST: nothing below may lengthen the time the export SA can read the secret.
  run "$GCLOUD" secrets remove-iam-policy-binding "$SECRET" --project "$PROD" \
    --member="serviceAccount:$PROD_EXPORT_SA" --role=roles/secretmanager.secretAccessor --quiet >/dev/null || {
    printf '\n!!! FAILED: could not remove %s access to %s in %s. The grant is STILL ON a prod secret.\n' \
      "$PROD_EXPORT_SA" "$SECRET" "$PROD" >&2
    printf '!!! Remove it by hand NOW: gcloud secrets remove-iam-policy-binding %s --project %s --member=serviceAccount:%s --role=roles/secretmanager.secretAccessor\n\n' \
      "$SECRET" "$PROD" "$PROD_EXPORT_SA" >&2
    failed=1
  }
  if [ "$rc" != 0 ] && [ "${EXECUTE_STARTED:-0}" = 1 ]; then
    # An interrupted or failed `execute --wait` can leave the execution RUNNING on prod (it keeps
    # the secret it already read after the grant goes). This job exists only for this run, so every
    # one of its executions is this run's: cancel them (--async: ask, don't wait) before the job is
    # deleted. A finished one refuses the cancel, which is harmless.
    for name in $(job_executions); do
      run "$GCLOUD" run jobs executions cancel "$name" --project "$PROD" --region "$REGION" --async --quiet >/dev/null 2>&1 \
        && echo "  cancelled execution $name" >&2 || echo "  execution $name: cancel refused (finished?)" >&2
    done
  fi
  if [ "${JOB_DEPLOYED:-0}" = 1 ]; then
    run "$GCLOUD" run jobs delete "$JOB" --project "$PROD" --region "$REGION" --quiet >/dev/null || {
      printf '\n!!! FAILED: could not delete job %s in %s (it carries %s). Delete it by hand: gcloud run jobs delete %s --project %s --region %s\n\n' \
        "$JOB" "$PROD" "$PROD_EXPORT_SA" "$JOB" "$PROD" "$REGION" >&2
      failed=1
    }
  fi
  return "$failed"
}
on_exit(){
  local rc=$?
  # IGNORE further signals while cleaning up (a second ^C or TERM must not skip the removal), and
  # the gcloud children inherit the ignore. `trap -` would restore the default, which kills.
  trap - EXIT
  trap '' INT TERM
  if ! cleanup "$rc"; then
    [ "$rc" = 0 ] && rc=3
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

run "$GCLOUD" secrets add-iam-policy-binding "$SECRET" --project "$PROD" \
  --member="serviceAccount:$PROD_EXPORT_SA" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
JOB_DEPLOYED=1   # set before the deploy: a deploy that half-created the job is still deleted
run "$GCLOUD" run jobs deploy "$JOB" --project "$PROD" --region "$REGION" --image "$IMG" \
  --service-account "$PROD_EXPORT_SA" \
  --network default --subnet default --vpc-egress all-traffic --max-retries 0 --task-timeout 3600s \
  --set-env-vars "PIVOTA_ENV=production,CATALOG_COPY_GCS_URI=gs://$DROP_BUCKET/catalog" \
  --set-secrets "DATABASE_URL=$SECRET:$VERSION" \
  --command python --args=-m,jobs.sandbox_catalog_copy,export --quiet
EXECUTE_STARTED=1
run "$GCLOUD" run jobs execute "$JOB" --project "$PROD" --region "$REGION" --wait
echo "  export executed; the EXIT trap now removes the grant and deletes the job; then R6, always"
