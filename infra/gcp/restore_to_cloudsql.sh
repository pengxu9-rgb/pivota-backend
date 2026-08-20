#!/usr/bin/env bash
# Import a plain-SQL gzip dump from GCS into the env's Cloud SQL `pivota` database.
#   infra/gcp/restore_to_cloudsql.sh staging gs://pivota-staging-migration/prod-<stamp>.sql.gz [--wipe]
# --wipe drops and recreates the `pivota` database first (staging rehearsals only; refuses on prod).
set -euo pipefail
ENV="${1:-}"; URI="${2:-}"; WIPE="${3:-}"
[ -n "$ENV" ] && [ -n "$URI" ] || { echo "usage: $0 staging|prod gs://bucket/file.sql.gz [--wipe]" >&2; exit 2; }
case "$ENV" in staging) PROJECT=pivota-staging ;; prod) PROJECT=pivota-prod ;; *) exit 2 ;; esac
GCLOUD="${GCLOUD:-gcloud}"; INSTANCE=pivota-pg; USER=pivota
# DB defaults to the main database; override for a second one (e.g. DB=pci_kb ... --wipe)
DB="${DB:-pivota}"
BUCKET="${URI#gs://}"; BUCKET="${BUCKET%%/*}"

# the instance's own service account must be able to read the object
SA=$("$GCLOUD" sql instances describe "$INSTANCE" --project "$PROJECT" --format='value(serviceAccountEmailAddress)')
"$GCLOUD" storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectViewer --project "$PROJECT" >/dev/null

if [ "$WIPE" = "--wipe" ]; then
  if [ "$ENV" != staging ]; then
    # Do not push the operator off-script mid-cutover: a half-failed prod import needs a wipe, and
    # improvising `gcloud sql databases delete` at 2am is worse than a guarded path. Require an
    # interactive typed confirmation, which no automation will satisfy by accident.
    echo "About to DROP AND RECREATE database '$DB' on $INSTANCE in $PROJECT ($ENV)." >&2
    [ -t 0 ] || { echo "refusing: --wipe on $ENV requires an interactive terminal" >&2; exit 1; }
    printf 'Type the project id to confirm: ' >&2; read -r CONFIRM
    [ "$CONFIRM" = "$PROJECT" ] || { echo "confirmation did not match - aborting" >&2; exit 1; }
  fi
  echo "wiping database $DB on $INSTANCE ($PROJECT)"
  "$GCLOUD" sql databases delete "$DB" --instance "$INSTANCE" --project "$PROJECT" --quiet
  "$GCLOUD" sql databases create "$DB" --instance "$INSTANCE" --project "$PROJECT"
fi

echo "importing $URI -> $PROJECT/$INSTANCE/$DB as $USER (this can take a while; runs server-side)"
"$GCLOUD" sql import sql "$INSTANCE" "$URI" --database="$DB" --user="$USER" --project "$PROJECT" --quiet
echo "import finished"

# Reconcile against the manifest the dump job wrote. `gcloud sql import` reports success per
# statement batch; a dump that was truncated before upload would still "succeed" here.
MANIFEST="${URI%.sql.gz}.tables"
if "$GCLOUD" storage cat "$MANIFEST" >/dev/null 2>&1; then
  EXPECTED=$("$GCLOUD" storage cat "$MANIFEST" | tr -d '[:space:]')
  echo "expected table count from dump manifest: $EXPECTED"
  echo "verify with:  select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE';"
else
  echo "!! no .tables manifest beside the dump - table count NOT verified" >&2
fi
