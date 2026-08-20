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
# Actually reconcile. Printing a SQL string for a human to run is not verification, and at cutover
# the final import is the one that has no second chance.
MANIFEST="${URI%.sql.gz}.tables"
"$GCLOUD" storage cat "$MANIFEST" >/dev/null 2>&1 \
  || { echo "FATAL: no .tables manifest beside the dump - cannot verify the import" >&2; exit 1; }
EXPECTED=$("$GCLOUD" storage cat "$MANIFEST" | tr -d '[:space:]')
COUNT_SQL="select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema') and table_schema not like 'pg_toast%' and table_type='BASE TABLE'"
JOB="verify-import-$$"
"$GCLOUD" run jobs create "$JOB" --region "$REGION" --project "$PROJECT" \
  --image "$REGION-docker.pkg.dev/pivota-shared/pivota/backend:latest" \
  --service-account "sa-worker@$PROJECT.iam.gserviceaccount.com" \
  --network default --subnet default --vpc-egress all-traffic \
  --set-secrets "DATABASE_URL=DATABASE_URL:latest" --max-retries 0 \
  --command psql --args "^|^\$DATABASE_URL|-Atc|$COUNT_SQL" --quiet >/dev/null 2>&1 || true
if "$GCLOUD" run jobs execute "$JOB" --region "$REGION" --project "$PROJECT" --wait --quiet >/dev/null 2>&1; then
  ACTUAL=$("$GCLOUD" logging read "resource.labels.job_name=\"$JOB\"" --project "$PROJECT" --limit 20 \
    --format='value(textPayload)' 2>/dev/null | grep -oE '^[0-9]+$' | head -1)
fi
"$GCLOUD" run jobs delete "$JOB" --region "$REGION" --project "$PROJECT" --quiet >/dev/null 2>&1 || true
if [ -z "${ACTUAL:-}" ]; then
  echo "!! could not read the imported table count - VERIFY BY HAND before cutting over:" >&2
  echo "   expected $EXPECTED tables; run: $COUNT_SQL" >&2
  exit 1
fi
echo "tables: expected=$EXPECTED imported=$ACTUAL"
[ "$ACTUAL" -ge "$EXPECTED" ] || { echo "FATAL: imported FEWER tables than the dump contained" >&2; exit 1; }
echo "import verified"
