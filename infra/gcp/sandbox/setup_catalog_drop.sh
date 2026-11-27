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
# --raw: gcloud's standardized describe output carries no project number at all (gcloud 581, found
# on the first apply, 2026-11-27); only the API's own resource has it, as `projectNumber`.
assert_drop_bucket_is_ours(){
  local bpn spn
  bpn=$("$GCLOUD" storage buckets describe "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" --raw --format=json 2>/dev/null \
        | python3 -c 'import json,re,sys; v=str(json.load(sys.stdin).get("projectNumber") or ""); print(v if re.fullmatch(r"[0-9]+", v) else "")' 2>/dev/null || true)
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

# The operator must hold roles/storage.admin on the sandbox project, granted directly to its own
# account and without a condition, before anything here changes the bucket. A project Owner reaches
# a bucket's IAM policy only through the bucket's legacy convenience bindings (projectOwner ->
# legacyBucketOwner), which side_sandbox removes: on the first apply (2026-11-27) the removal took
# the operator's getIamPolicy/setIamPolicy away mid-run, so the reader grant and the remaining
# removals failed and the bucket was left half-done. A group grant is not seen here: grant it to
# the account itself. Dry run: says what it will check.
assert_operator_is_storage_admin(){
  local acct pol verdict
  acct=$("$GCLOUD" config get-value account 2>/dev/null | tr 'A-Z' 'a-z' || true)
  pol=$("$GCLOUD" projects get-iam-policy "$SANDBOX_PROJECT" --format=json 2>/dev/null || true)
  if [ -z "$acct" ] || [ -z "$pol" ]; then
    [ "$APPLY" = 1 ] && { echo "refusing: cannot read the operator account or $SANDBOX_PROJECT's IAM policy, so roles/storage.admin cannot be confirmed" >&2; exit 1; }
    echo "  [dry-run] before any change: the operator account must hold roles/storage.admin on $SANDBOX_PROJECT (directly, no condition)"
    return 0
  fi
  verdict=$(printf '%s' "$pol" | ACCT="$acct" python3 -c '
import json, os, sys
acct = os.environ["ACCT"]
want = ("serviceAccount:" if acct.endswith(".gserviceaccount.com") else "user:") + acct
ok = any(b.get("role") == "roles/storage.admin" and not b.get("condition")
         and want in [m.lower() for m in b.get("members", [])]
         for b in json.load(sys.stdin).get("bindings", []))
print("ok" if ok else "missing")' 2>/dev/null || true)
  if [ "$verdict" != ok ]; then
    if [ "$APPLY" = 1 ]; then
      echo "refusing: $acct does not hold roles/storage.admin on $SANDBOX_PROJECT (directly, no condition). Project Owner is not enough: it reaches the bucket's IAM only through the legacy bindings this script removes. RUNBOOK step 7 says how to grant it." >&2
      exit 1
    fi
    echo "  [dry-run] would refuse: $acct does not hold roles/storage.admin on $SANDBOX_PROJECT (RUNBOOK step 7)"
    return 0
  fi
  echo "  operator $acct holds roles/storage.admin on $SANDBOX_PROJECT"
}

side_sandbox(){
  assert_operator_is_storage_admin
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
  # The reader FIRST, then the legacy removals: if a removal fails part-way, the import can still
  # read, and a re-run finishes the removals (each step is idempotent).
  run "$GCLOUD" storage buckets add-iam-policy-binding "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" \
    --member="serviceAccount:$SA_IMPORT" --role=roles/storage.objectViewer
  echo "  reader: $SA_IMPORT (objectViewer)"
  # Remove the project-convenience legacy bindings: any roles/storage.legacy* role held by
  # projectViewer/projectEditor/projectOwner of this project (a new bucket's default policy).
  # The policy is read once. Before the bucket exists (a dry run on a fresh project) there is no
  # policy to read, so the plan says what will be removed instead of reading nothing silently.
  # With APPLY=1 the bucket exists by now, and a failed read stops the run rather than skipping.
  local policy legacy
  policy=$("$GCLOUD" storage buckets get-iam-policy "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" --format=json 2>/dev/null || true)
  if [ -z "$policy" ]; then
    [ "$APPLY" = 1 ] && { echo "could not read the IAM policy of gs://$DROP_BUCKET - stopping before the legacy removals" >&2; exit 1; }
    echo "  [dry-run] after the bucket exists: remove every roles/storage.legacy* binding held by projectViewer/projectEditor/projectOwner:$SANDBOX_PROJECT (read from its policy then)"
  else
    legacy=$(printf '%s' "$policy" | legacy_bindings)
    while read -r role member; do
      [ -n "$role" ] || continue
      run "$GCLOUD" storage buckets remove-iam-policy-binding "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" \
        --member="$member" --role="$role"
    done <<<"$legacy"
  fi
  # Applied: read the policy back. No legacy convenience binding may remain, and the reader must be there.
  if [ "$APPLY" = 1 ]; then
    policy=$("$GCLOUD" storage buckets get-iam-policy "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" --format=json 2>/dev/null || true)
    [ -n "$policy" ] || { echo "could not read back the IAM policy of gs://$DROP_BUCKET" >&2; exit 1; }
    legacy=$(printf '%s' "$policy" | legacy_bindings)
    [ -z "$legacy" ] || { printf 'legacy bindings remain on gs://%s:\n%s\n' "$DROP_BUCKET" "$legacy" >&2; exit 1; }
    printf '%s' "$policy" | M="serviceAccount:$SA_IMPORT" python3 -c '
import json, os, sys
sys.exit(0 if any(b.get("role") == "roles/storage.objectViewer" and os.environ["M"] in b.get("members", [])
                  for b in json.load(sys.stdin).get("bindings", [])) else 1)' \
      || { echo "the reader binding for $SA_IMPORT is missing on gs://$DROP_BUCKET" >&2; exit 1; }
    echo "  read back: no legacy convenience binding, reader present"
  fi
  echo "  the prod writer is a separate step: $0 prod-writer"
}

# stdin: a bucket IAM policy (JSON). Prints "<role> <member>" for every roles/storage.legacy* role
# held by this project's projectViewer/projectEditor/projectOwner convenience members.
legacy_bindings(){
  PROJ="$SANDBOX_PROJECT" python3 -c '
import json, os, sys
proj = os.environ["PROJ"]
d = json.load(sys.stdin)
for b in d.get("bindings", []):
    if b.get("role", "").startswith("roles/storage.legacy"):
        for m in b.get("members", []):
            if m in (f"projectViewer:{proj}", f"projectEditor:{proj}", f"projectOwner:{proj}"):
                print(b["role"], m)'
}

side_prod_writer(){
  # THIS IS A PROD CHANGE (a new service account in pivota-prod). Own go, own controller slot.
  # The SA gets nothing in pivota-prod from this script. Its prod-side grants (read of a
  # catalog-only DB credential, run of the export job) belong to the export job's own setup,
  # which sandbox-backend writes; see RUNBOOK.md Q3 for the read-only DB role recommendation.
  # Every check before the first change, so a refusal leaves no prod SA without its grant.
  local name="${PROD_EXPORT_SA%%@*}"
  assert_operator_is_storage_admin
  have "$GCLOUD" storage buckets describe "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" || [ "$APPLY" != 1 ] \
    || { echo "bucket gs://$DROP_BUCKET does not exist - run '$0 sandbox' first" >&2; exit 1; }
  assert_drop_bucket_is_ours
  have "$GCLOUD" iam service-accounts describe "$PROD_EXPORT_SA" --project pivota-prod \
    || run "$GCLOUD" iam service-accounts create "$name" --project pivota-prod \
         --display-name="exports catalog tables to the partner sandbox drop (write-only)"
  retry "$GCLOUD" storage buckets add-iam-policy-binding "gs://$DROP_BUCKET" --project "$SANDBOX_PROJECT" \
    --member="serviceAccount:$PROD_EXPORT_SA" --role=roles/storage.objectCreator
  echo "  writer: $PROD_EXPORT_SA (objectCreator: create only; no read, list, delete or overwrite)"
}

case "$SIDE" in
  sandbox) side_sandbox ;;
  prod-writer) side_prod_writer ;;
  *) echo "usage: $0 sandbox|prod-writer   (APPLY=1 to execute)" >&2; exit 2 ;;
esac
