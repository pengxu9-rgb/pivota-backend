#!/usr/bin/env bash
# RUNBOOK step 10a-prod: run the prod schema dump once, as a PROD one-off (controller + owner go).
#
#   infra/gcp/sandbox/schema_dump_prod.sh <dump image by digest>            # dry run
#   APPLY=1 infra/gcp/sandbox/schema_dump_prod.sh <dump image by digest>    # the gated run
#
# The export SA may read the dump role's secret ONLY for the duration of this script. The grant is
# added first and removed by an EXIT trap, so it goes on every path: success, a failed deploy, a
# failed execution, set -e, or ^C (review T3: a pasted last line was skipped when a step failed).
# The exit status is the run's, unless the removal fails: then the script ALWAYS exits non-zero
# (3 when the run itself succeeded), with a loud line, because a grant left on a prod secret is a
# failed step however the dump went.
set -euo pipefail
# shellcheck source=infra/gcp/sandbox/config.sh
. "$(cd "$(dirname "$0")" && pwd)/config.sh"
PROD=pivota-prod
SECRET=SANDBOX_SCHEMA_DUMP_DATABASE_URL
DUMP_IMG="${1:-}"
[[ "$DUMP_IMG" =~ ^us-west1-docker\.pkg\.dev/pivota-shared/pivota/sandbox-schema-dump@sha256:[0-9a-f]{64}$ ]] \
  || { echo "usage: $0 us-west1-docker.pkg.dev/pivota-shared/pivota/sandbox-schema-dump@sha256:<digest>" >&2; exit 2; }
say "schema dump in $PROD -> gs://$DROP_BUCKET/schema"; mode_banner

remove_grant(){
  run "$GCLOUD" secrets remove-iam-policy-binding "$SECRET" --project "$PROD" \
    --member="serviceAccount:$PROD_EXPORT_SA" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
}
on_exit(){
  local rc=$?
  trap - EXIT INT TERM
  if ! remove_grant; then
    printf '\n!!! FAILED: could not remove %s access to %s in %s. The grant is STILL ON a prod secret.\n' \
      "$PROD_EXPORT_SA" "$SECRET" "$PROD" >&2
    printf '!!! Remove it by hand NOW: gcloud secrets remove-iam-policy-binding %s --project %s --member=serviceAccount:%s --role=roles/secretmanager.secretAccessor\n\n' \
      "$SECRET" "$PROD" "$PROD_EXPORT_SA" >&2
    [ "$rc" = 0 ] && rc=3
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

run "$GCLOUD" secrets add-iam-policy-binding "$SECRET" --project "$PROD" \
  --member="serviceAccount:$PROD_EXPORT_SA" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
run "$GCLOUD" run jobs deploy sandbox-schema-dump --project "$PROD" --region "$REGION" --image "$DUMP_IMG" \
  --service-account "$PROD_EXPORT_SA" \
  --network default --subnet default --vpc-egress all-traffic --max-retries 0 --task-timeout 1800s \
  --set-env-vars "PIVOTA_ENV=production,SANDBOX_SCHEMA_GCS_URI=gs://$DROP_BUCKET/schema" \
  --set-secrets "DATABASE_URL=$SECRET:latest" --quiet
run "$GCLOUD" run jobs execute sandbox-schema-dump --project "$PROD" --region "$REGION" --wait
echo "  dump executed; the EXIT trap now removes the grant"
