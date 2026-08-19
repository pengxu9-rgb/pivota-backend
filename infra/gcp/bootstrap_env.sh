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
  prod)    PROJECT=pivota-prod;    SQL_TIER=db-custom-2-7680; SQL_HA=REGIONAL; REDIS_GB=2; REDIS_TIER=standard_ha; DELETION_PROTECTION=--deletion-protection ;;
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

log "[$ENV] project=$PROJECT region=$REGION"
"$GCLOUD" config set project "$PROJECT" >/dev/null
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
  if ! have "$GCLOUD" secrets describe "$name"; then
    "$GCLOUD" secrets create "$name" --replication-policy=automatic >/dev/null
    "$@" | "$GCLOUD" secrets versions add "$name" --data-file=- >/dev/null
    echo "  created $name"
  else
    echo "  exists  $name"
  fi
}
ensure_secret pivota-db-password sh -c 'openssl rand -base64 36 | tr -d "/+=\n"'
DB_PASSWORD=$("$GCLOUD" secrets versions access latest --secret=pivota-db-password)
if ! "$GCLOUD" sql users list --instance="$SQL_INSTANCE" --format='value(name)' | grep -qx "$DB_USER"; then
  "$GCLOUD" sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD"
fi
SQL_PRIVATE_IP=$("$GCLOUD" sql instances describe "$SQL_INSTANCE" --format='value(ipAddresses[0].ipAddress)')
SQL_CONN=$("$GCLOUD" sql instances describe "$SQL_INSTANCE" --format='value(connectionName)')
ensure_secret DATABASE_URL sh -c "printf 'postgresql://$DB_USER:$DB_PASSWORD@$SQL_PRIVATE_IP:5432/$DB_NAME?sslmode=require'"
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
ensure_secret REDIS_URL sh -c "printf 'redis://:%s@$REDIS_HOST:$REDIS_PORT/0' \"\$($GCLOUD redis instances get-auth-string $REDIS_INSTANCE --region=$REGION --format='value(authString)')\""

# ---------------------------------------------------------------- service accounts + IAM
log "Service accounts + IAM"
for sa in "${SERVICE_ACCOUNTS[@]}"; do
  EMAIL="$sa@$PROJECT.iam.gserviceaccount.com"
  have "$GCLOUD" iam service-accounts describe "$EMAIL" || "$GCLOUD" iam service-accounts create "$sa" --display-name="Cloud Run $sa"
  for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/logging.logWriter roles/monitoring.metricWriter roles/cloudtrace.agent; do
    "$GCLOUD" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$EMAIL" --role="$role" --condition=None >/dev/null
  done
  # pull images from the shared registry
  "$GCLOUD" artifacts repositories add-iam-policy-binding pivota --location="$REGION" --project="$SHARED" \
    --member="serviceAccount:$EMAIL" --role=roles/artifactregistry.reader >/dev/null
done
# Cloud Run's own service agent must also read the shared registry
"$GCLOUD" artifacts repositories add-iam-policy-binding pivota --location="$REGION" --project="$SHARED" \
  --member="serviceAccount:service-$PROJECT_NUMBER@serverless-robot-prod.iam.gserviceaccount.com" --role=roles/artifactregistry.reader >/dev/null

# ---------------------------------------------------------------- summary (no secrets)
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
