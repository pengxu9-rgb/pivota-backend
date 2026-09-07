#!/usr/bin/env bash
# Import a plain-SQL gzip dump from GCS into the env's Cloud SQL `pivota` database.
#   infra/gcp/restore_to_cloudsql.sh staging gs://pivota-prod-migration/prod-<stamp>.sql.gz [--wipe]
#   (dumps live in the PROD bucket even when restoring INTO staging - the staging Cloud SQL service
#    account is granted objectViewer on it. Prod data must not sit in a staging-project bucket.)
# --wipe drops and recreates the `pivota` database first (staging rehearsals only; refuses on prod).
#
# CUTOVER PATH: --new-db, not --wipe.
# The rehearsal on 2026-08-20 showed wipe-in-place is the wrong shape for a cutover. DROP DATABASE
# fails while ANY session holds the database, and scaling Cloud Run to zero does not kill running
# instances promptly - three separate attempts failed with "database is being accessed by other
# users" even after draining and terminating backends. Fighting that at 2am, against a clock, with
# the old data already dropped, is the worst possible position.
#
# --new-db imports into a FRESH database (pivota_<stamp>) while the services keep running on the
# current one. Nothing is dropped, nothing must be drained, and the switch is a DATABASE_URL secret
# update plus a redeploy. The previous database stays intact as an instant rollback: point the
# secret back and redeploy. That is the sequence CUTOVER.md now documents.
set -euo pipefail
ENV="${1:-}"; URI="${2:-}"; WIPE="${3:-}"
# --new-db: import into pivota_<stamp> and leave the live database untouched.
if [ "$WIPE" = "--new-db" ]; then
  # Name from the dump's stamp PLUS the current time: deriving it from the URI alone makes a re-run
  # against the same dump collide with the database the previous run created, and the import then
  # fails on "schema already exists" rather than doing anything useful.
  NEW_DB="pivota_$(date -u +%m%d%H%M)_$(printf '%s' "$URI" | tr -cd '[:alnum:]' | tail -c 6)"
  DB="$NEW_DB"; WIPE=""
  NEW_DB_MODE=1
else
  NEW_DB_MODE=0
fi
[ -n "$ENV" ] && [ -n "$URI" ] || { echo "usage: $0 staging|prod gs://bucket/file.sql.gz [--wipe]" >&2; exit 2; }
case "$ENV" in staging) PROJECT=pivota-staging ;; prod) PROJECT=pivota-prod ;; *) exit 2 ;; esac
GCLOUD="${GCLOUD:-gcloud}"; INSTANCE=pivota-pg; USER=pivota
REGION="${REGION:-us-west1}"   # needed by the Cloud Run drain below
# DB defaults to the main database; override for a second one (e.g. DB=pci_kb ... --wipe)
DB="${DB:-pivota}"
BUCKET="${URI#gs://}"; BUCKET="${BUCKET%%/*}"

# the instance's own service account must be able to read the object
SA=$("$GCLOUD" sql instances describe "$INSTANCE" --project "$PROJECT" --format='value(serviceAccountEmailAddress)')
"$GCLOUD" storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectViewer --project "$PROJECT" >/dev/null

# A DROP DATABASE fails while anything holds a connection - "database is being accessed by other
# users". Every Cloud Run service here runs min-instances >= 1 and holds a pool, so the wipe fails
# unless they are drained first. This is not optional at cutover: the final re-sync MUST wipe,
# because importing over a populated database fails on the first CREATE SCHEMA.
# Draining is free during the cutover window - DNS still points at Railway, so nothing is serving.
drain_services(){
  local dir="$1" svc mins
  for svc in $("$GCLOUD" run services list --project "$PROJECT" --region "$REGION" --format='value(metadata.name)'); do
    if [ "$dir" = down ]; then
      # `spec.template` IS THE RIGHT READ HERE. This captures a value to PUT BACK later with
      # `run services update --min-instances`, which sets the template — so the template is
      # what must be recorded. The serving revision's annotation would restore whatever a
      # half-finished deploy happened to leave running. (See infra/gcp/_serving_revision.sh for
      # the opposite question and when to use it.)
      mins=$("$GCLOUD" run services describe "$svc" --project "$PROJECT" --region "$REGION" \
        --format="value(spec.template.metadata.annotations['autoscaling.knative.dev/minScale'])")
      echo "${svc}=${mins:-0}" >> "$SCALE_STATE"
      "$GCLOUD" run services update "$svc" --project "$PROJECT" --region "$REGION" --min-instances 0 --quiet >/dev/null 2>&1 \
        && echo "   drained $svc (was min=${mins:-0})"
    fi
  done
}
restore_scale(){
  [ -f "$SCALE_STATE" ] || return 0
  local line svc mins
  while IFS= read -r line; do
    svc="${line%%=*}"; mins="${line##*=}"
    [ "$mins" = 0 ] && continue
    "$GCLOUD" run services update "$svc" --project "$PROJECT" --region "$REGION" --min-instances "$mins" --quiet >/dev/null 2>&1 \
      && echo "   restored $svc min=$mins"
  done < "$SCALE_STATE"
  rm -f "$SCALE_STATE"
}
SCALE_STATE=$(mktemp)
trap 'restore_scale' EXIT INT TERM

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
  echo "draining Cloud Run services so the DROP can proceed"
  drain_services down
  echo "waiting 30s for pooled connections to close"; sleep 30
  # Scaling to zero is not enough: Cloud Run does not kill a running instance immediately, and a
  # DROP DATABASE fails while ANY session remains. Terminate them server-side from a session on the
  # `postgres` database (you cannot drop the database you are connected to), then drop.
  echo "terminating any remaining sessions on $DB"
  ADMIN_DSN=$("$GCLOUD" secrets versions access latest --secret=DATABASE_URL --project "$PROJECT" | sed "s|/$DB?|/postgres?|")
  TJOB="killconns-$$"
  "$GCLOUD" run jobs create "$TJOB" --region "$REGION" --project "$PROJECT" \
    --image "$REGION-docker.pkg.dev/pivota-shared/pivota/backend:latest" \
    --service-account "sa-worker@$PROJECT.iam.gserviceaccount.com" \
    --network default --subnet default --vpc-egress all-traffic \
    --set-env-vars "ADMIN_DSN=$ADMIN_DSN,TARGET_DB=$DB" --max-retries 0 --task-timeout 120s \
    --command python --args="^|^-c|import asyncio,os,asyncpg
async def m():
    c=await asyncpg.connect(os.environ['ADMIN_DSN'].split('?')[0], ssl='require')
    n=await c.fetch(\"select pg_terminate_backend(pid) from pg_stat_activity where datname=\$1 and pid<>pg_backend_pid()\", os.environ['TARGET_DB'])
    print('TERMINATED='+str(len(n)))
    await c.close()
asyncio.run(m())" --quiet >/dev/null 2>&1
  "$GCLOUD" run jobs execute "$TJOB" --region "$REGION" --project "$PROJECT" --wait --quiet >/dev/null 2>&1 || true
  "$GCLOUD" run jobs delete "$TJOB" --region "$REGION" --project "$PROJECT" --quiet >/dev/null 2>&1 || true
  echo "wiping database $DB on $INSTANCE ($PROJECT)"
  "$GCLOUD" sql databases delete "$DB" --instance "$INSTANCE" --project "$PROJECT" --quiet
  "$GCLOUD" sql databases create "$DB" --instance "$INSTANCE" --project "$PROJECT"
fi

if [ "$NEW_DB_MODE" = 1 ]; then
  "$GCLOUD" sql databases list --instance="$INSTANCE" --project "$PROJECT" --format='value(name)' | grep -qx "$DB" \
    || "$GCLOUD" sql databases create "$DB" --instance="$INSTANCE" --project "$PROJECT" --quiet
  echo "importing into FRESH database $DB - the live database is untouched"
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
# python+asyncpg, NOT psql: this image installs libpq5 but no postgresql-client, so a `--command psql`
# job never starts and the count comes back empty (the script then correctly fails closed, but the
# verification never actually runs). Also point at the database we imported into, which with --new-db
# is not the one DATABASE_URL names.
VERIFY_DSN=$("$GCLOUD" secrets versions access latest --secret=DATABASE_URL --project "$PROJECT" | sed "s|/pivota?|/$DB?|")
PYSRC="import asyncio,os,asyncpg
async def m():
    c=await asyncpg.connect(os.environ['VERIFY_DSN'].split('?')[0], ssl='require')
    n=await c.fetchval(\"select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema') and table_schema not like 'pg_toast%' and table_type='BASE TABLE'\")
    print('TABLE_COUNT='+str(n))
    await c.close()
asyncio.run(m())"
"$GCLOUD" run jobs create "$JOB" --region "$REGION" --project "$PROJECT" \
  --image "$REGION-docker.pkg.dev/pivota-shared/pivota/backend:latest" \
  --service-account "sa-worker@$PROJECT.iam.gserviceaccount.com" \
  --network default --subnet default --vpc-egress all-traffic \
  --set-env-vars "VERIFY_DSN=$VERIFY_DSN" --max-retries 0 --task-timeout 180s \
  --command python --args="^|^-c|$PYSRC" --quiet >/dev/null 2>&1
if "$GCLOUD" run jobs execute "$JOB" --region "$REGION" --project "$PROJECT" --wait --quiet >/dev/null 2>&1; then
  for i in 1 2 3 4 5 6; do
    ACTUAL=$("$GCLOUD" logging read "resource.labels.job_name=\"$JOB\"" --project "$PROJECT" --limit 20 \
      --format='value(textPayload)' 2>/dev/null | grep -oE 'TABLE_COUNT=[0-9]+' | head -1 | cut -d= -f2)
    [ -n "${ACTUAL:-}" ] && break
    sleep 5
  done
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
if [ "$NEW_DB_MODE" = 1 ]; then
  echo
  echo "  Switch to it when ready (and this is also the rollback, in reverse):"
  echo "    OLD=\$($GCLOUD secrets versions access latest --secret=DATABASE_URL --project $PROJECT)"
  echo "    printf '%s' \"\${OLD/\\/pivota?//$DB?}\" | $GCLOUD secrets versions add DATABASE_URL --data-file=- --project $PROJECT"
  echo "    then redeploy the services so they pick up the new secret version"
fi
