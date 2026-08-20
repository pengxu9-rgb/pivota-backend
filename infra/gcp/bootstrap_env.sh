#!/usr/bin/env bash
# Bootstrap one Pivota environment (staging|prod) on Google Cloud.
# Idempotent: every step checks for the resource before creating it.
#
#   usage: infra/gcp/bootstrap_env.sh staging|prod
#
# Decisions this encodes (see ~/dev/GCP_MIGRATION_URL_AUDIT_2026-08-19.md and memory):
#   - region us-west1 (same as Railway today; best US region for APAC reach)
#   - ONE Cloud SQL PG17 instance per env (Railway prod is 17.10, 3.9 GB, 365 tables;
#     extensions pgcrypto/pg_trgm/pg_stat_statements); private IP only, on the default VPC
#   - ONE Memorystore Redis per env (Railway has one redis, used by `web` only)
#   - images live in pivota-shared Artifact Registry; both envs pull the same tags
#   - secrets go to Secret Manager; nothing is printed
set -euo pipefail

ENV="${1:-}"
case "$ENV" in
  staging) PROJECT=pivota-staging; SQL_TIER=db-custom-1-3840; SQL_HA=zonal;    REDIS_GB=1; REDIS_TIER=basic;       DELETION_PROTECTION=--no-deletion-protection ;;
  # Memorystore's HA tier is spelled `standard` (it is the tier WITH a replica); `standard_ha` is the
  # API enum, and gcloud normalizes it to `standard-ha`, which is not a valid --tier choice. Staging
  # uses `basic`, so this line only ever executes on the prod path - it failed there first.
  prod)    PROJECT=pivota-prod;    SQL_TIER=db-custom-2-7680; SQL_HA=REGIONAL; REDIS_GB=2; REDIS_TIER=standard;    DELETION_PROTECTION=--deletion-protection ;;
  *) echo "usage: $0 staging|prod" >&2; exit 2 ;;
esac

REGION=us-west1
SHARED=pivota-shared
NETWORK=default
GCLOUD="${GCLOUD:-gcloud}"
SQL_INSTANCE=pivota-pg
REDIS_INSTANCE=pivota-redis
DB_NAME=pivota
DB_USER=pivota
SERVICE_ACCOUNTS=(sa-backend sa-gateway sa-worker)

log(){ printf '\n\033[1;34m== %s\033[0m\n' "$*"; }
have(){ "$@" >/dev/null 2>&1; }
# IAM is eventually consistent: a freshly created service account can be rejected by the
# policy API for ~10-30s ("does not exist"). Retry with backoff instead of failing the run.
retry(){ local n=0; until "$@"; do n=$((n+1)); [ $n -ge 8 ] && return 1; sleep $((n*5)); done; }

log "[$ENV] project=$PROJECT region=$REGION"
# Scope the project to THIS process only. `gcloud config set project` persists in ~/.config/gcloud
# and would leave the operator's next hand-typed command pointed at whatever env ran last.
export CLOUDSDK_CORE_PROJECT="$PROJECT"
DRIFT=0
PROJECT_NUMBER=$("$GCLOUD" projects describe "$PROJECT" --format='value(projectNumber)')

# ---------------------------------------------------------------- shared: Artifact Registry
log "Artifact Registry repo pivota in $SHARED"
if ! have "$GCLOUD" artifacts repositories describe pivota --location="$REGION" --project="$SHARED"; then
  "$GCLOUD" artifacts repositories create pivota --repository-format=docker --location="$REGION" \
    --project="$SHARED" --description="Pivota service images (shared by staging+prod)"
fi

# ---------------------------------------------------------------- private services access (Cloud SQL / Memorystore private IP)
log "Private services access on VPC $NETWORK"
if ! have "$GCLOUD" compute addresses describe google-managed-services-"$NETWORK" --global; then
  "$GCLOUD" compute addresses create google-managed-services-"$NETWORK" --global --purpose=VPC_PEERING \
    --prefix-length=16 --network="$NETWORK" --description="PSA range for Cloud SQL / Memorystore"
fi
if ! "$GCLOUD" services vpc-peerings list --network="$NETWORK" --format='value(peering)' 2>/dev/null | grep -q servicenetworking-googleapis-com; then
  "$GCLOUD" services vpc-peerings connect --service=servicenetworking.googleapis.com \
    --ranges=google-managed-services-"$NETWORK" --network="$NETWORK"
fi

# ---------------------------------------------------------------- Cloud SQL
log "Cloud SQL $SQL_INSTANCE (POSTGRES_17, $SQL_TIER, $SQL_HA, private IP)"
if ! have "$GCLOUD" sql instances describe "$SQL_INSTANCE"; then
  "$GCLOUD" sql instances create "$SQL_INSTANCE" \
    --database-version=POSTGRES_17 --edition=ENTERPRISE --tier="$SQL_TIER" --region="$REGION" \
    --availability-type="$SQL_HA" \
    --network="projects/$PROJECT/global/networks/$NETWORK" --no-assign-ip \
    --storage-type=SSD --storage-size=20 --storage-auto-increase \
    --backup-start-time=09:00 --enable-point-in-time-recovery --retained-backups-count=14 \
    --maintenance-window-day=SUN --maintenance-window-hour=10 \
    --database-flags=max_connections=200 \
    $DELETION_PROTECTION
fi
if ! "$GCLOUD" sql databases list --instance="$SQL_INSTANCE" --format='value(name)' | grep -qx "$DB_NAME"; then
  "$GCLOUD" sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"
fi

# ---------------------------------------------------------------- secrets (generated once, never printed)
log "Secret Manager: db password + DATABASE_URL + redis auth"
ensure_secret(){ # name, generator-command
  local name="$1"; shift
  have "$GCLOUD" secrets describe "$name" || "$GCLOUD" secrets create "$name" --replication-policy=automatic >/dev/null
  # Check VERSIONS, not just the secret: a previous run that failed mid-way can leave a secret with
  # zero (or empty) versions, and "the secret exists" would then hide it forever.
  if "$GCLOUD" secrets versions access latest --secret="$name" 2>/dev/null | grep -q .; then
    echo "  exists  $name"; return 0
  fi
  # Generate to a temp file FIRST and validate: the generator runs in its own shell without -e or
  # pipefail, so `openssl ... | tr ...` can fail and still exit 0 with empty output, which would
  # otherwise be written as a valid-looking empty secret (empty DB password, unusable DATABASE_URL).
  local tmp; tmp=$(mktemp); chmod 600 "$tmp"
  if ! "$@" > "$tmp" || ! [ -s "$tmp" ]; then rm -f "$tmp"; echo "FAILED to generate a value for $name" >&2; exit 1; fi
  "$GCLOUD" secrets versions add "$name" --data-file="$tmp" >/dev/null
  rm -f "$tmp"
  echo "  created $name"
}
ensure_secret pivota-db-password sh -c 'openssl rand -base64 36 | tr -d "/+=\n"'
DB_PASSWORD=$("$GCLOUD" secrets versions access latest --secret=pivota-db-password)
if ! "$GCLOUD" sql users list --instance="$SQL_INSTANCE" --format='value(name)' | grep -qx "$DB_USER"; then
  "$GCLOUD" sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD"
fi
SQL_PRIVATE_IP=$("$GCLOUD" sql instances describe "$SQL_INSTANCE" --format='value(ipAddresses[0].ipAddress)')
SQL_CONN=$("$GCLOUD" sql instances describe "$SQL_INSTANCE" --format='value(connectionName)')
ensure_secret DATABASE_URL sh -c "printf 'postgresql://$DB_USER:$DB_PASSWORD@$SQL_PRIVATE_IP:5432/$DB_NAME?sslmode=require'"
# ensure_secret never OVERWRITES an existing value, so a recreated instance (new private IP) would
# leave this secret pointing at a dead address. Detect the drift loudly rather than silently serving it.
if ! "$GCLOUD" secrets versions access latest --secret=DATABASE_URL | grep -qF "@$SQL_PRIVATE_IP:5432/"; then
  echo "  !! DATABASE_URL does not point at $SQL_PRIVATE_IP - Cloud SQL was recreated." >&2
  echo "     Add a new version deliberately, then redeploy every service:" >&2
  echo "       $GCLOUD secrets versions add DATABASE_URL --data-file=- --project $PROJECT" >&2
  DRIFT=1
fi
unset DB_PASSWORD

# ---------------------------------------------------------------- Memorystore Redis
log "Memorystore $REDIS_INSTANCE (${REDIS_GB}GB $REDIS_TIER, AUTH on)"
if ! have "$GCLOUD" redis instances describe "$REDIS_INSTANCE" --region="$REGION"; then
  "$GCLOUD" redis instances create "$REDIS_INSTANCE" --region="$REGION" --size="$REDIS_GB" --tier="$REDIS_TIER" \
    --redis-version=redis_7_2 --network="projects/$PROJECT/global/networks/$NETWORK" \
    --connect-mode=PRIVATE_SERVICE_ACCESS --enable-auth \
    --redis-config maxmemory-policy=allkeys-lru
fi
REDIS_HOST=$("$GCLOUD" redis instances describe "$REDIS_INSTANCE" --region="$REGION" --format='value(host)')
REDIS_PORT=$("$GCLOUD" redis instances describe "$REDIS_INSTANCE" --region="$REGION" --format='value(port)')
if have "$GCLOUD" secrets describe REDIS_URL && ! "$GCLOUD" secrets versions access latest --secret=REDIS_URL | grep -qF "@$REDIS_HOST:$REDIS_PORT/"; then
  echo "  !! REDIS_URL does not point at $REDIS_HOST:$REDIS_PORT - Memorystore was recreated." >&2
  DRIFT=1
fi
# Fetch and validate the auth string FIRST. Building the URL inside the generator defeats
# ensure_secret's non-empty check: a failed get-auth-string still produces "redis://:@host:6379/0",
# which is non-empty, looks valid, and would be stored forever (ensure_secret never overwrites).
REDIS_AUTH=$("$GCLOUD" redis instances get-auth-string "$REDIS_INSTANCE" --region="$REGION" --format='value(authString)' 2>/dev/null || true)
[ -n "$REDIS_AUTH" ] || { echo "FAILED to read the Memorystore auth string - refusing to store an auth-less REDIS_URL" >&2; exit 1; }
ensure_secret REDIS_URL printf 'redis://:%s@%s:%s/0' "$REDIS_AUTH" "$REDIS_HOST" "$REDIS_PORT"
unset REDIS_AUTH

# ---------------------------------------------------------------- service accounts + IAM
log "Service accounts + IAM"
for sa in "${SERVICE_ACCOUNTS[@]}"; do
  EMAIL="$sa@$PROJECT.iam.gserviceaccount.com"
  have "$GCLOUD" iam service-accounts describe "$EMAIL" || "$GCLOUD" iam service-accounts create "$sa" --display-name="Cloud Run $sa"
  for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/logging.logWriter roles/monitoring.metricWriter roles/cloudtrace.agent; do
    retry "$GCLOUD" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$EMAIL" --role="$role" --condition=None >/dev/null 2>&1 \
      || { echo "FAILED binding $role to $EMAIL" >&2; exit 1; }
  done
  # pull images from the shared registry
  retry "$GCLOUD" artifacts repositories add-iam-policy-binding pivota --location="$REGION" --project="$SHARED" \
    --member="serviceAccount:$EMAIL" --role=roles/artifactregistry.reader >/dev/null 2>&1 \
    || { echo "FAILED registry binding for $EMAIL" >&2; exit 1; }
done
# Cloud Run's own service agent must also read the shared registry
# On a project where Cloud Run has never been deployed this service agent does not exist yet, so the
# binding fails and set -e would abort the run AFTER Cloud SQL and Memorystore were already created.
retry "$GCLOUD" artifacts repositories add-iam-policy-binding pivota --location="$REGION" --project="$SHARED" \
  --member="serviceAccount:service-$PROJECT_NUMBER@serverless-robot-prod.iam.gserviceaccount.com" --role=roles/artifactregistry.reader >/dev/null 2>&1 \
  || echo "  !! could not grant the Cloud Run service agent registry read - deploy once, then re-run this script" >&2

# ---------------------------------------------------------------- summary (no secrets)
[ "$DRIFT" = 0 ] || echo "
!! DRIFT DETECTED (see above) - secrets do not match the live resources. Fix before deploying." >&2
log "Done: $ENV"
cat <<SUMMARY
  project            $PROJECT ($PROJECT_NUMBER)
  registry           $REGION-docker.pkg.dev/$SHARED/pivota
  cloud sql          $SQL_CONN  private_ip=$SQL_PRIVATE_IP  db=$DB_NAME user=$DB_USER
  redis              $REDIS_HOST:$REDIS_PORT
  secrets            pivota-db-password, DATABASE_URL, REDIS_URL
  service accounts   ${SERVICE_ACCOUNTS[*]} (@${PROJECT}.iam.gserviceaccount.com)
  cloud run must run with --vpc-egress=all-traffic / direct VPC egress on '$NETWORK' to reach SQL+Redis,
  and with PIVOTA_ENV=$( [ "$ENV" = prod ] && echo production || echo staging ) (platform shim fails closed without it)
SUMMARY
