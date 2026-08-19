#!/usr/bin/env bash
# Import a plain-SQL gzip dump from GCS into the env's Cloud SQL `pivota` database.
#   infra/gcp/restore_to_cloudsql.sh staging gs://pivota-staging-migration/prod-<stamp>.sql.gz [--wipe]
# --wipe drops and recreates the `pivota` database first (staging rehearsals only; refuses on prod).
set -euo pipefail
ENV="${1:-}"; URI="${2:-}"; WIPE="${3:-}"
[ -n "$ENV" ] && [ -n "$URI" ] || { echo "usage: $0 staging|prod gs://bucket/file.sql.gz [--wipe]" >&2; exit 2; }
case "$ENV" in staging) PROJECT=pivota-staging ;; prod) PROJECT=pivota-prod ;; *) exit 2 ;; esac
GCLOUD="${GCLOUD:-gcloud}"; INSTANCE=pivota-pg; DB=pivota; USER=pivota
BUCKET="${URI#gs://}"; BUCKET="${BUCKET%%/*}"

# the instance's own service account must be able to read the object
SA=$("$GCLOUD" sql instances describe "$INSTANCE" --project "$PROJECT" --format='value(serviceAccountEmailAddress)')
"$GCLOUD" storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectViewer --project "$PROJECT" >/dev/null

if [ "$WIPE" = "--wipe" ]; then
  [ "$ENV" = staging ] || { echo "refusing --wipe on $ENV" >&2; exit 1; }
  echo "wiping database $DB on $INSTANCE ($PROJECT)"
  "$GCLOUD" sql databases delete "$DB" --instance "$INSTANCE" --project "$PROJECT" --quiet
  "$GCLOUD" sql databases create "$DB" --instance "$INSTANCE" --project "$PROJECT"
fi

echo "importing $URI -> $PROJECT/$INSTANCE/$DB as $USER (this can take a while; runs server-side)"
"$GCLOUD" sql import sql "$INSTANCE" "$URI" --database="$DB" --user="$USER" --project "$PROJECT" --quiet
echo "import finished"
