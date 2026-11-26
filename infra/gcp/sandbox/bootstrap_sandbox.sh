#!/usr/bin/env bash
# Build the partner sandbox's GCP project, one phase at a time.
#
#   infra/gcp/sandbox/bootstrap_sandbox.sh <phase>          # dry run: prints what it would do
#   APPLY=1 infra/gcp/sandbox/bootstrap_sandbox.sh <phase>  # does it (controller + owner go)
#
# phases, in runbook order (RUNBOOK.md steps 1-5):
#   project   create the project in the org, link billing, enable APIs
#   network   custom VPC + subnet (Private Google Access), PSA peering, Cloud NAT on a reserved IP
#   sql       Cloud SQL Postgres 17 (private IP only) + database + user
#   secrets   DB secrets, minted internal keys, the EMPTY REAP_API_KEY container
#   iam       service accounts, per-secret access, logging/metrics roles, registry read
#
# Idempotent: every create is preceded by a describe. Nothing here touches pivota-prod or
# pivota-staging; the only other project it writes to is pivota-shared, and only to grant the
# sandbox's service accounts READ on the image registry (phase `iam`).
#
# Secrets: values are generated in this process from openssl's CSPRNG straight into
# `gcloud secrets versions add --data-file`, through a 0600 temp file that is removed at once.
# Nothing is printed, and no value is read back except DATABASE_URL's host part, which is
# checked without being echoed.
set -euo pipefail
# shellcheck source=infra/gcp/sandbox/config.sh
. "$(cd "$(dirname "$0")" && pwd)/config.sh"

PHASE="${1:-}"
case "$PHASE" in project|network|sql|secrets|iam) ;; *)
  echo "usage: $0 project|network|sql|secrets|iam   (APPLY=1 to execute)" >&2; exit 2 ;;
esac
# Every secret-bearing temp file lives in ONE private directory that is removed on any exit,
# including an error or ^C halfway through a phase.
SBX_TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/sbx-secrets.XXXXXX"); chmod 700 "$SBX_TMPDIR"
# EXIT cleans up. INT/TERM clean up AND stop: a trap that only cleans up returns to the script,
# which then carries on (review T1: after ^C it went on to `gcloud secrets create`).
trap 'rm -rf "$SBX_TMPDIR"' EXIT
trap 'rm -rf "$SBX_TMPDIR"; echo "interrupted - stopped" >&2; exit 130' INT TERM
say "sandbox bootstrap: phase=$PHASE project=$SANDBOX_PROJECT region=$REGION"
mode_banner

project_number(){ g projects describe "$SANDBOX_PROJECT" --format='value(projectNumber)' 2>/dev/null || echo '<PROJECT_NUMBER>'; }

# ------------------------------------------------------------------------------------- project
phase_project(){
  : "${BILLING_ACCOUNT:?set BILLING_ACCOUNT=<id> (the account in infra/gcp/README.md)}"
  if have "$GCLOUD" projects describe "$SANDBOX_PROJECT"; then
    echo "  exists  project $SANDBOX_PROJECT"
  else
    # Project ids are global. If this fails with "already in use", pick another id
    # (SANDBOX_PROJECT=pivota-partner-sandbox) and re-run; every script reads SANDBOX_PROJECT.
    run "$GCLOUD" projects create "$SANDBOX_PROJECT" --organization="$ORG_ID" \
      --name="Pivota partner sandbox" --labels=env=sandbox,owner=pivota
  fi
  run "$GCLOUD" billing projects link "$SANDBOX_PROJECT" --billing-account="$BILLING_ACCOUNT"
  # Only what the sandbox uses. No Vertex AI / Gemini: the sandbox makes no LLM calls (env profile).
  run g services enable \
    run.googleapis.com compute.googleapis.com sqladmin.googleapis.com \
    servicenetworking.googleapis.com secretmanager.googleapis.com \
    certificatemanager.googleapis.com cloudscheduler.googleapis.com \
    logging.googleapis.com monitoring.googleapis.com iam.googleapis.com \
    storage.googleapis.com artifactregistry.googleapis.com
  # A new project with compute enabled gets an auto-mode `default` VPC with permissive
  # internal firewall rules unless org policy skips it. The sandbox uses its own VPC only;
  # delete `default` so nothing lands on it by accident. (Only if it exists and is empty.)
  if have g compute networks describe default; then
    for fw in $(g compute firewall-rules list --filter='network~/default$' --format='value(name)' 2>/dev/null); do
      run g compute firewall-rules delete "$fw" --quiet
    done
    run g compute networks delete default --quiet
  fi
}

# ------------------------------------------------------------------------------------- network
phase_network(){
  have g compute networks describe "$VPC" \
    || run g compute networks create "$VPC" --subnet-mode=custom \
         --description="partner sandbox: Cloud Run direct VPC egress, Cloud SQL private IP"
  have g compute networks subnets describe "$SUBNET" --region "$REGION" \
    || run g compute networks subnets create "$SUBNET" --network "$VPC" --region "$REGION" \
         --range "$SUBNET_RANGE" --enable-private-ip-google-access
  # Private services access for Cloud SQL's private IP.
  have g compute addresses describe "$PSA_RANGE_NAME" --global \
    || run g compute addresses create "$PSA_RANGE_NAME" --global --purpose=VPC_PEERING \
         --addresses="$PSA_RANGE_START" --prefix-length=20 --network="$VPC" \
         --description="PSA range for sandbox Cloud SQL"
  if ! grep -q servicenetworking-googleapis-com <<<"$(g services vpc-peerings list --network="$VPC" --format='value(peering)' 2>/dev/null)"; then
    run g services vpc-peerings connect --service=servicenetworking.googleapis.com \
      --ranges="$PSA_RANGE_NAME" --network="$VPC"
  fi
  # Cloud NAT on a RESERVED address. Every service runs --vpc-egress=all-traffic (the gateway must
  # reach `web` through the VPC because web's ingress is `internal`), so internet egress - the
  # Reap sandbox API - leaves through this NAT. One fixed address means: (a) if Reap allowlists
  # source IPs, there is exactly one to give them; (b) a later egress firewall (RUNBOOK.md, Q8)
  # has a single choke point. This address is the SANDBOX's; never give a partner staging's or prod's.
  have g compute addresses describe "$EGRESS_IP_NAME" --region "$REGION" \
    || run g compute addresses create "$EGRESS_IP_NAME" --region "$REGION" \
         --description="partner sandbox egress (Cloud NAT)"
  have g compute routers describe "$ROUTER" --region "$REGION" \
    || run g compute routers create "$ROUTER" --network "$VPC" --region "$REGION"
  have g compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" \
    || run g compute routers nats create "$NAT" --router "$ROUTER" --region "$REGION" \
         --nat-custom-subnet-ip-ranges="$SUBNET" --nat-external-ip-pool="$EGRESS_IP_NAME" \
         --enable-logging --log-filter=ERRORS_ONLY
}

# ------------------------------------------------------------------------------------- sql
phase_sql(){
  if ! have g sql instances describe "$SQL_INSTANCE"; then
    # Private IP only. No PITR: the data is a daily copy plus test orders, and 7 daily backups
    # cover a bad import. Deletion protection on, so a mistyped delete cannot drop it.
    run g sql instances create "$SQL_INSTANCE" \
      --database-version=POSTGRES_17 --edition=ENTERPRISE --tier="$SQL_TIER" --region="$REGION" \
      --availability-type=zonal \
      --network="projects/$SANDBOX_PROJECT/global/networks/$VPC" --no-assign-ip \
      --storage-type=SSD --storage-size=20 --storage-auto-increase \
      --backup-start-time=09:00 --retained-backups-count=7 \
      --maintenance-window-day=SUN --maintenance-window-hour=10 \
      --deletion-protection
  fi
  if ! grep -qx "$DB_NAME" <<<"$(g sql databases list --instance="$SQL_INSTANCE" --format='value(name)' 2>/dev/null)"; then
    run g sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"
  fi
  echo "  (the DB user is created in phase 'secrets', from the generated password)"
}

# ------------------------------------------------------------------------------------- secrets
ensure_container(){ # name origin
  local name="$1" origin="$2"
  have g secrets describe "$name" \
    || run g secrets create "$name" --replication-policy=automatic \
         --labels="env=sandbox,origin=$origin"
}
has_version(){ grep -q . <<<"$(g secrets versions list "$1" --filter='state=ENABLED' --limit=1 --format='value(name)' 2>/dev/null)"; }
add_generated(){ # name origin generator...
  local name="$1" origin="$2"; shift 2
  ensure_container "$name" "$origin"
  if has_version "$name"; then echo "  exists  $name"; return 0; fi
  if [ "$APPLY" != 1 ]; then echo "  [dry-run] generate a value and add it as the first version of $name"; return 0; fi
  local tmp; tmp=$(mktemp "$SBX_TMPDIR/v.XXXXXX"); chmod 600 "$tmp"
  if ! "$@" > "$tmp" || ! [ -s "$tmp" ]; then rm -f "$tmp"; echo "FAILED to generate $name" >&2; exit 1; fi
  g secrets versions add "$name" --data-file="$tmp" >/dev/null
  rm -f "$tmp"
  echo "  created $name"
}
rand_token(){ openssl rand -base64 48 | tr -d '/+=\n'; }
# SCRAM-SHA-256 verifier (RFC 7677, Postgres' stored format) of the password in $1, computed here so
# only the verifier ever reaches the database. The password goes to python on STDIN, never an argv.
scram_verifier(){ printf '%s' "$1" | python3 -c '
import base64, hashlib, hmac, os, sys
pw = sys.stdin.read().encode(); salt = os.urandom(16); it = 4096
salted = hashlib.pbkdf2_hmac("sha256", pw, salt, it)
ck = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
sk = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
b64 = lambda b: base64.b64encode(b).decode()
print(f"SCRAM-SHA-256${it}:{b64(salt)}${b64(hashlib.sha256(ck).digest())}:{b64(sk)}", end="")'; }
# The gateway's own service key must be in the sandbox key format (ak_test_<64 hex>): the gateway's
# boot check refuses a live-format key, and the MCP door's loopback call presents this key.
ak_test_key(){ printf 'ak_test_%s' "$(openssl rand -hex 32)"; }

minted_secret_names(){ # every secret named in secrets/*.list that is not DB or Reap
  cat "$HERE_SANDBOX"/secrets/*.list | sed 's/^[^=]*=//' | sort -u \
    | grep -vxE 'DATABASE_URL|DATABASE_URL_NOVERIFY|SCHEMA_APPLY_DATABASE_URL|SCHEMA_APPLY_ROLE_VERIFIER|REAP_API_KEY'
}

# `gcloud sql users create --password=...` would put the password on gcloud's argv (visible in
# `ps`, and possibly in gcloud's own logs). The Cloud SQL Admin API takes it in a request BODY
# instead: the body and the auth header are 0600 files in $SBX_TMPDIR, and curl reads both with @.
# No external process gets the password or the token as an argument.
create_db_user_without_argv_password(){
  local pwf="$SBX_TMPDIR/pw" body="$SBX_TMPDIR/body.json" hdr="$SBX_TMPDIR/auth.hdr" n=0
  ( umask 077
    g secrets versions access latest --secret=pivota-db-password --out-file="$pwf"
    python3 -c 'import json,sys; print(json.dumps({"name": sys.argv[1], "password": open(sys.argv[2]).read()}))' \
      "$DB_USER" "$pwf" > "$body"
    { printf 'Authorization: Bearer '; "$GCLOUD" auth print-access-token; } > "$hdr" )
  # -q FIRST: never read ~/.curlrc, where a -v/--trace would print the Authorization header (T2).
  curl -q -sS --fail -o /dev/null -X POST -H @"$hdr" -H 'Content-Type: application/json' --data-binary @"$body" \
    "https://sqladmin.googleapis.com/v1/projects/$SANDBOX_PROJECT/instances/$SQL_INSTANCE/users" \
    || { rm -f "$pwf" "$body" "$hdr"; echo "FAILED to create DB user $DB_USER" >&2; exit 1; }
  rm -f "$pwf" "$body" "$hdr"
  # The API answers with an operation; wait until the user is listed.
  until g sql users list --instance="$SQL_INSTANCE" --format='value(name)' | awk -v u="$DB_USER" '$0==u{f=1} END{exit !f}'; do
    n=$((n+1)); [ $n -le 24 ] || { echo "DB user $DB_USER not listed after 2 min" >&2; exit 1; }; sleep 5
  done
}

phase_secrets(){
  have g sql instances describe "$SQL_INSTANCE" || [ "$APPLY" != 1 ] \
    || { echo "Cloud SQL $SQL_INSTANCE does not exist - run phase sql first" >&2; exit 1; }

  say "DB credentials (sandbox Cloud SQL only)"
  add_generated pivota-db-password sandbox-cloudsql rand_token
  if [ "$APPLY" = 1 ]; then
    local pw ip
    pw=$(g secrets versions access latest --secret=pivota-db-password)
    local users; users=$(g sql users list --instance="$SQL_INSTANCE" --format='value(name)')
    if ! grep -qx "$DB_USER" <<<"$(printf '%s\n' "$users")"; then
      create_db_user_without_argv_password
      echo "  created DB user $DB_USER"
    fi
    ip=$(g sql instances describe "$SQL_INSTANCE" --format='value(ipAddresses[0].ipAddress)')
    [ -n "$ip" ] || { echo "no private IP on $SQL_INSTANCE yet" >&2; exit 1; }
    # asyncpg (web/worker/import) verifies nothing under sslmode=require; node-pg (gateway) needs
    # no-verify for the same behaviour - infra/gcp/README.md "sslmode=require does NOT mean...".
    add_generated DATABASE_URL sandbox-cloudsql printf 'postgresql://%s:%s@%s:5432/%s?sslmode=require' "$DB_USER" "$pw" "$ip" "$DB_NAME"
    add_generated DATABASE_URL_NOVERIFY sandbox-cloudsql printf 'postgresql://%s:%s@%s:5432/%s?sslmode=no-verify' "$DB_USER" "$pw" "$ip" "$DB_NAME"
    unset pw
    # The schema-apply role (RUNBOOK 10a-sandbox): its DSN for the apply job, and the SCRAM-SHA-256
    # VERIFIER of the same password for the role job. The role is created with the verifier only:
    # Postgres logs a failing CREATE ROLE statement server-side, so the plaintext never goes in one.
    # Both are minted together or not at all, so they can never disagree.
    ensure_container SCHEMA_APPLY_DATABASE_URL sandbox-cloudsql
    ensure_container SCHEMA_APPLY_ROLE_VERIFIER sandbox-cloudsql
    local has_dsn=0 has_ver=0
    has_version SCHEMA_APPLY_DATABASE_URL && has_dsn=1
    has_version SCHEMA_APPLY_ROLE_VERIFIER && has_ver=1
    if [ "$has_dsn$has_ver" = 11 ]; then
      echo "  exists  SCHEMA_APPLY_DATABASE_URL + SCHEMA_APPLY_ROLE_VERIFIER"
    elif [ "$has_dsn$has_ver" = 00 ]; then
      local apw; apw=$(rand_token)
      add_generated SCHEMA_APPLY_DATABASE_URL sandbox-cloudsql printf 'postgresql://sandbox_schema_apply:%s@%s:5432/%s?sslmode=require' "$apw" "$ip" "$DB_NAME"
      add_generated SCHEMA_APPLY_ROLE_VERIFIER sandbox-cloudsql scram_verifier "$apw"
      unset apw
    else
      echo "only one of SCHEMA_APPLY_DATABASE_URL / SCHEMA_APPLY_ROLE_VERIFIER has an enabled version: disable both, then re-run" >&2
      exit 1
    fi
    # Drift check without echoing: the stored URL must point at THIS instance's private IP.
    grep -qF "@$ip:5432/" <<<"$(g secrets versions access latest --secret=DATABASE_URL)" \
      || { echo "!! DATABASE_URL does not point at $SQL_INSTANCE ($ip). Add a new version deliberately." >&2; exit 1; }
  else
    echo "  [dry-run] create DB user $DB_USER; add DATABASE_URL, DATABASE_URL_NOVERIFY and SCHEMA_APPLY_DATABASE_URL for $SQL_INSTANCE's private IP"
  fi

  say "Reap SANDBOX key: container only (the owner adds the value - RUNBOOK.md step 4)"
  ensure_container REAP_API_KEY reap-sandbox
  has_version REAP_API_KEY && echo "  REAP_API_KEY has a version" || echo "  REAP_API_KEY has NO version yet (expected until step 4)"

  say "internal keys, minted here (never copied from staging or prod)"
  local n
  for n in $(minted_secret_names); do
    if [ "$n" = PIVOTA_API_KEY ]; then add_generated "$n" minted-in-sandbox ak_test_key
    else add_generated "$n" minted-in-sandbox rand_token; fi
  done
}

# ------------------------------------------------------------------------------------- iam
sa_for_list(){ # secrets/<name>.list -> service account email
  case "$1" in
    web) echo "$SA_BACKEND" ;; worker) echo "$SA_WORKER" ;; gateway) echo "$SA_GATEWAY" ;;
    agenpaz) echo "$SA_AGENPAZ" ;; catalog-import|schema-apply) echo "$SA_IMPORT" ;; ops) echo "$SA_OPS" ;;
    *) echo "unknown secrets list $1" >&2; exit 1 ;;
  esac
}
phase_iam(){
  local sa email role list secret pn
  for sa in sa-gateway sa-backend sa-worker sa-agenpaz sa-catalog-import sa-sandbox-ops; do
    email="$sa@$SANDBOX_PROJECT.iam.gserviceaccount.com"
    have g iam service-accounts describe "$email" \
      || run g iam service-accounts create "$sa" --display-name="sandbox $sa"
    # Project-level roles are observability only. No secretmanager.secretAccessor at project
    # level (granted per secret below), no cloudsql.client (private IP, password auth), no editor.
    for role in roles/logging.logWriter roles/monitoring.metricWriter roles/cloudtrace.agent; do
      retry g projects add-iam-policy-binding "$SANDBOX_PROJECT" --member="serviceAccount:$email" \
        --role="$role" --condition=None --quiet >/dev/null
    done
  done

  say "the default Compute service account loses its automatic roles/editor"
  # A new project grants <N>-compute@developer.gserviceaccount.com roles/editor (unless org policy
  # iam.automaticIamGrantsForDefaultServiceAccounts is enforced). Nothing in the sandbox runs as it,
  # and verify_sandbox.sh's iam group fails while it holds editor.
  pn=$(project_number)
  local dce="serviceAccount:${pn}-compute@developer.gserviceaccount.com" has_editor
  has_editor=$(g projects get-iam-policy "$SANDBOX_PROJECT" --format=json 2>/dev/null | M="$dce" python3 -c '
import json,os,sys
try: d=json.load(sys.stdin)
except Exception: print("unknown"); sys.exit(0)
print("yes" if any(b.get("role")=="roles/editor" and os.environ["M"] in b.get("members",[]) for b in d.get("bindings",[])) else "no")' || echo unknown)
  case "$has_editor" in
    yes) run g projects remove-iam-policy-binding "$SANDBOX_PROJECT" --member="$dce" --role=roles/editor --quiet >/dev/null ;;
    no) echo "  $dce holds no editor grant" ;;
    *) if [ "$APPLY" = 1 ]; then echo "could not read the project policy to check $dce" >&2; exit 1; fi
       echo "  [dry-run] remove roles/editor from $dce if the project grants it (read after the project exists)" ;;
  esac

  say "per-secret access, from secrets/*.list"
  for list in "$HERE_SANDBOX"/secrets/*.list; do
    local base; base=$(basename "$list" .list); email=$(sa_for_list "$base")
    for secret in $(sed 's/^[^=]*=//' "$list" | sort -u); do
      retry g secrets add-iam-policy-binding "$secret" --member="serviceAccount:$email" \
        --role=roles/secretmanager.secretAccessor --quiet >/dev/null
    done
  done

  say "image registry read in $SHARED_PROJECT (the ONLY write this script makes outside the sandbox)"
  pn=$(project_number)
  for repo in pivota pivota-gateway; do
    for email in "$SA_GATEWAY" "$SA_BACKEND" "$SA_WORKER" "$SA_AGENPAZ" "$SA_IMPORT" "$SA_OPS" \
                 "service-$pn@serverless-robot-prod.iam.gserviceaccount.com"; do
      retry "$GCLOUD" artifacts repositories add-iam-policy-binding "$repo" --location="$REGION" \
        --project="$SHARED_PROJECT" --member="serviceAccount:$email" \
        --role=roles/artifactregistry.reader --quiet >/dev/null
    done
  done
  # The Cloud Run service agent exists only after the run API is enabled (phase project).
  echo "  if the serverless-robot binding failed, run phase iam again after the first deploy"
}

"phase_$PHASE"
say "done: $PHASE ($( [ "$APPLY" = 1 ] && echo applied || echo dry run ))"
