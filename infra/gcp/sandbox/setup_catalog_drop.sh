#!/usr/bin/env bash
# The one-way catalog drop: prod export WRITES, sandbox import READS, nothing else.
#
#   infra/gcp/sandbox/setup_catalog_drop.sh sandbox          # bucket + reader (sandbox project)
#   infra/gcp/sandbox/setup_catalog_drop.sh prod-writer      # the prod export SA + its write grant
#   (dry run unless APPLY=1; each side is its own runbook step and its own go - RUNBOOK.md 9a/9b)
#
# Why the bucket lives in pivota-SANDBOX and not pivota-prod: the sandbox must hold no permission
# on anything in prod. With the bucket here, the only cross-project grant is prod -> sandbox, and
# it is write-only:
#
#   principal                                   on                     role
#   sa-sandbox-export@pivota-prod               gs://<drop bucket>     roles/storage.objectCreator
#   sa-catalog-import@pivota-sandbox            gs://<drop bucket>     roles/storage.objectViewer
#
# objectCreator can create objects but not read, list, delete or overwrite them, so the prod
# exporter cannot read anything back out of the sandbox, and every export must use a new object
# name (catalog/<stamp>/...). The sandbox's import job reads the drop and writes ONLY to the
# sandbox's own Cloud SQL, with the sandbox's own DATABASE_URL. No prod DB credential ever exists
# in pivota-sandbox: the export runs in pivota-prod, as a prod job, holding prod's credential there.
#
# Lesson carried over from infra/gcp/README.md item 11: a bucket's default IAM policy grants
# project viewers legacy read. That convenience binding is removed here, so being a Viewer on the
# sandbox project does not mean reading the drop. Catalog rows carry no buyer PII (the export's
# table allowlist is sandbox-backend's design), but the bucket is built as if they could.
set -euo pipefail
# shellcheck source=infra/gcp/sandbox/config.sh
. "$(cd "$(dirname "$0")" && pwd)/config.sh"
SIDE="${1:-}"
say "catalog drop: side=$SIDE bucket=gs://$DROP_BUCKET"; mode_banner

# The bucket must belong to THIS project (by project NUMBER) before anything here changes it. The
# name alone proves nothing: bucket names are global, and a same-named bucket elsewhere would
# otherwise get our delete lifecycle and IAM edits. Dry run: says what it will check.
assert_drop_bucket_is_ours(){
  local bpn spn
  bpn=$("$GCLOUD" storage buckets describe "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" --format=json 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("project_number") or d.get("projectNumber") or "")' 2>/dev/null || true)
  spn=$("$GCLOUD" projects describe "$SANDBOX_PROJECT" --format='value(projectNumber)' 2>/dev/null || true)
  if [ -z "$bpn" ]; then
    [ "$APPLY" = 1 ] && { echo "cannot read the project number of gs://$DROP_BUCKET - refusing to change it" >&2; exit 1; }
    echo "  [dry-run] before any change: gs://$DROP_BUCKET must belong to $SANDBOX_PROJECT's project number"
    return 0
  fi
  if [ -z "$spn" ] || [ "$bpn" != "$spn" ]; then
    echo "refusing: gs://$DROP_BUCKET belongs to project number $bpn, not $SANDBOX_PROJECT (${spn:-unknown})" >&2; exit 1
  fi
  echo "  gs://$DROP_BUCKET belongs to $SANDBOX_PROJECT ($spn)"
}

side_sandbox(){
  if ! have "$GCLOUD" storage buckets describe "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT"; then
    run "$GCLOUD" storage buckets create "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" \
      --location="$REGION" --uniform-bucket-level-access --public-access-prevention \
      --default-storage-class=STANDARD
  fi
  assert_drop_bucket_is_ours
  # Drops older than 7 days are deleted; the import only ever wants the newest complete drop.
  local lc; lc=$(mktemp); trap 'rm -f "$lc"' RETURN
  printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}\n' > "$lc"
  run "$GCLOUD" storage buckets update "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" --lifecycle-file="$lc"
  # Remove the project-convenience legacy bindings: any roles/storage.legacy* role held by
  # projectViewer/projectEditor/projectOwner of this project (a new bucket's default policy).
  # The policy is read once. Before the bucket exists (a dry run on a fresh project) there is no
  # policy to read, so the plan says what will be removed instead of reading nothing silently.
  # With APPLY=1 the bucket exists by now, and a failed read stops the run rather than skipping.
  local policy legacy
  policy=$("$GCLOUD" storage buckets get-iam-policy "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" --format=json 2>/dev/null || true)
  if [ -z "$policy" ]; then
    [ "$APPLY" = 1 ] && { echo "could not read the IAM policy of gs://$DROP_BUCKET - stopping before the reader grant" >&2; exit 1; }
    echo "  [dry-run] after the bucket exists: remove every roles/storage.legacy* binding held by projectViewer/projectEditor/projectOwner:$SANDBOX_PROJECT (read from its policy then)"
  else
    legacy=$(printf '%s' "$policy" | PROJ="$SANDBOX_PROJECT" python3 -c '
import json, os, sys
proj = os.environ["PROJ"]
d = json.load(sys.stdin)
for b in d.get("bindings", []):
    if b.get("role", "").startswith("roles/storage.legacy"):
        for m in b.get("members", []):
            if m in (f"projectViewer:{proj}", f"projectEditor:{proj}", f"projectOwner:{proj}"):
                print(b["role"], m)')
    while read -r role member; do
      [ -n "$role" ] || continue
      run "$GCLOUD" storage buckets remove-iam-policy-binding "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" \
        --member="$member" --role="$role"
    done <<<"$legacy"
  fi
  run "$GCLOUD" storage buckets add-iam-policy-binding "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" \
    --member="serviceAccount:$SA_IMPORT" --role=roles/storage.objectViewer
  echo "  reader: $SA_IMPORT (objectViewer)"
  echo "  the prod writer is a separate step: $0 prod-writer"
}

side_prod_writer(){
  # THIS IS A PROD CHANGE (a new service account in pivota-prod). Own go, own controller slot.
  # The SA gets nothing in pivota-prod from this script. Its prod-side grants (read of a
  # catalog-only DB credential, run of the export job) belong to the export job's own setup,
  # which sandbox-backend writes; see RUNBOOK.md Q3 for the read-only DB role recommendation.
  local name="${PROD_EXPORT_SA%%@*}"
  have "$GCLOUD" iam service-accounts describe "$PROD_EXPORT_SA" --project pivota-prod \
    || run "$GCLOUD" iam service-accounts create "$name" --project pivota-prod \
         --display-name="exports catalog tables to the partner sandbox drop (write-only)"
  have "$GCLOUD" storage buckets describe "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" || [ "$APPLY" != 1 ] \
    || { echo "bucket gs://$DROP_BUCKET does not exist - run '$0 sandbox' first" >&2; exit 1; }
  assert_drop_bucket_is_ours
  retry "$GCLOUD" storage buckets add-iam-policy-binding "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" \
    --member="serviceAccount:$PROD_EXPORT_SA" --role=roles/storage.objectCreator
  echo "  writer: $PROD_EXPORT_SA (objectCreator: create only; no read, list, delete or overwrite)"
}

case "$SIDE" in
  sandbox) side_sandbox ;;
  prod-writer) side_prod_writer ;;
  *) echo "usage: $0 sandbox|prod-writer   (APPLY=1 to execute)" >&2; exit 2 ;;
esac
