#!/usr/bin/env bash
# The sandbox schema-apply role (RUNBOOK 10a-sandbox; spec: sandbox-backend's schema-apply role).
# Runs INSIDE a one-off Cloud Run job on the digest-pinned postgres:17 image, in the sandbox VPC,
# as sa-sandbox-ops, with three mounted secrets:
#   DATABASE_URL                the sandbox's ADMIN connection (the app owner role, from bootstrap)
#   SCHEMA_APPLY_DATABASE_URL   the apply role's DSN (only its user and database name are read here)
#   SCHEMA_APPLY_ROLE_VERIFIER  the SCRAM-SHA-256 verifier of the apply role's password
#
#   bash -c "<this file>" schema_apply_role create   before 10a-sandbox's apply
#   bash -c "<this file>" schema_apply_role retire   after the apply's DONE
#   bash -c "<this file>" schema_apply_role drop     recovery only (see RUNBOOK 10a-sandbox)
#
# The role is created with the VERIFIER only. Postgres writes a failing statement to its server
# log, so a plaintext password must never be part of one (review: a second `create` logged it).
# The verifier reaches psql only through the environment (\getenv), never an argv.
# Roles are CLUSTER-wide: dropping and re-creating the database does not remove this role, so
# `create` refuses when it already exists and says how to recover.
# This file must never contain two at-signs in a row: it reaches the job through gcloud's
# alternate argument delimiter made of exactly that (RUNBOOK 10a-sandbox; a test checks it).
set -euo pipefail
MODE="${1:-}"
[ "${PIVOTA_ENV:-}" = sandbox ] || { echo "REFUSED: runs only where PIVOTA_ENV=sandbox" >&2; exit 2; }
[ -n "${DATABASE_URL:-}" ] && [ -n "${SCHEMA_APPLY_DATABASE_URL:-}" ] || { echo "REFUSED: both DSNs must be mounted" >&2; exit 2; }
case "$MODE" in create|retire|drop) ;; *) echo "usage: create|retire|drop" >&2; exit 2 ;; esac

# postgresql://sandbox_schema_apply:<pw>@<host>:5432/<db>?...: only the user and the db name are taken.
rest="${SCHEMA_APPLY_DATABASE_URL#postgresql://}"
userinfo="${rest%%@*}"; after="${rest#*@}"
apply_user="${userinfo%%:*}"
db="${after#*/}"; db="${db%%\?*}"
[ "$apply_user" = sandbox_schema_apply ] || { echo "REFUSED: the apply DSN must name sandbox_schema_apply" >&2; exit 2; }
[ "${userinfo#*:}" != "$userinfo" ] && [ -n "${userinfo#*:}" ] || { echo "REFUSED: the apply DSN has no password" >&2; exit 2; }
[[ "$db" =~ ^[a-z_][a-z0-9_]*$ ]] || { echo "REFUSED: unexpected database name" >&2; exit 2; }
export SBX_DB="$db"

# Does the role exist? A plain read; no secret in it.
exists=$(psql "$DATABASE_URL" -X -tAq -c "select exists (select 1 from pg_roles where rolname = 'sandbox_schema_apply')")

if [ "$MODE" = create ]; then
  if [ "$exists" = t ]; then
    cat >&2 <<'MSG'
REFUSED: role sandbox_schema_apply already exists. Roles are cluster-wide and survive a DROP/CREATE
DATABASE, so this is left from an earlier attempt. Recover: run this job in `drop` mode, then run
`create` again with the same secrets (RUNBOOK 10a-sandbox, rollback). Nothing was changed.
MSG
    exit 2
  fi
  [ -n "${SCHEMA_APPLY_ROLE_VERIFIER:-}" ] || { echo "REFUSED: SCHEMA_APPLY_ROLE_VERIFIER is not mounted" >&2; exit 2; }
  # RFC 7677 shape with real lengths (review: `SCRAM-SHA-256$4096:AAAA$AAAA:AAAA` matched a looser
  # pattern, and Postgres then stored the string as a PLAINTEXT password's hash): base64 salt of at
  # least 16 bytes (>= 6 full quads, or >= 5 quads plus a padded tail), and StoredKey and ServerKey
  # of exactly 32 bytes each (43 base64 characters and one `=`).
  b64q='[A-Za-z0-9+/]{4}'
  salt_re="(($b64q){5,}([A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)|($b64q){6,})"
  key_re='[A-Za-z0-9+/]{43}='
  scram_re="^SCRAM-SHA-256[\$]4096:${salt_re}[\$]${key_re}:${key_re}\$"
  [[ "$SCHEMA_APPLY_ROLE_VERIFIER" =~ $scram_re ]] \
    || { echo "REFUSED: SCHEMA_APPLY_ROLE_VERIFIER is not a SCRAM-SHA-256 verifier (4096 iterations, salt >= 16 bytes, 32-byte keys; a plaintext password is never sent)" >&2; exit 2; }
  export SBX_APPLY_VERIFIER="$SCHEMA_APPLY_ROLE_VERIFIER"
  psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 --single-transaction -q -f - <<'SQL'
\getenv apply_verifier SBX_APPLY_VERIFIER
\getenv db SBX_DB
SELECT current_user AS admin \gset
-- 1. login and nothing else; no membership of any role (not cloudsqlsuperuser, not the app role).
--    The PASSWORD is a SCRAM verifier: Postgres stores it as is, and it is all a log could show.
CREATE ROLE sandbox_schema_apply LOGIN NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB
  NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2 PASSWORD :'apply_verifier';
-- 2. connect, and create objects in public only (it owns only what it creates). NOT CREATE on the
--    database: that would let it create schemas and trusted extensions.
GRANT CONNECT ON DATABASE :"db" TO sandbox_schema_apply;
--    No TEMP for PUBLIC (sandbox-backend d66c349c9: a temp relation is searched before pg_catalog,
--    so it could stand in for a catalog the apply job reads; the job refuses a role with TEMP). The
--    app owner role (this admin) gets TEMP back: the catalog copy (10c) stages rows in TEMP tables.
REVOKE TEMPORARY ON DATABASE :"db" FROM PUBLIC;
GRANT TEMPORARY ON DATABASE :"db" TO :"admin";
GRANT USAGE, CREATE ON SCHEMA public TO sandbox_schema_apply;
-- 3. the fixed extension list (backend: pg_trgm, pgcrypto; gateway: vector); the dump never creates
--    one. Confirm against the first dump's CREATE EXTENSION lines before applying.
CREATE EXTENSION IF NOT EXISTS pg_trgm  WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS vector   WITH SCHEMA public;
-- 4. fail the whole transaction unless the role is exactly as specified
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member = 'sandbox_schema_apply'::regrole) THEN
    RAISE EXCEPTION 'sandbox_schema_apply is a member of a role';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sandbox_schema_apply'
             AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)) THEN
    RAISE EXCEPTION 'sandbox_schema_apply has an attribute it must not have';
  END IF;
  IF has_database_privilege('sandbox_schema_apply', current_database(), 'CREATE') THEN
    RAISE EXCEPTION 'sandbox_schema_apply may create in the database';
  END IF;
  IF has_database_privilege('sandbox_schema_apply', current_database(), 'TEMP') THEN
    RAISE EXCEPTION 'sandbox_schema_apply may create temp objects (PUBLIC still has TEMPORARY?)';
  END IF;
  IF NOT has_database_privilege(current_user, current_database(), 'TEMP') THEN
    RAISE EXCEPTION 'the app owner role lost TEMP (the catalog copy needs it)';
  END IF;
  -- no per-role setting may exist on the role (never ALTER it with SET): the job refuses one
  IF EXISTS (SELECT 1 FROM pg_db_role_setting WHERE setrole = 'sandbox_schema_apply'::regrole) THEN
    RAISE EXCEPTION 'sandbox_schema_apply has a per-role setting';
  END IF;
END $$;
SQL
  echo "DONE create: sandbox_schema_apply (login, no membership, no TEMP, no per-role setting, USAGE+CREATE on public, 3 extensions)"
elif [ "$MODE" = retire ]; then
  [ "$exists" = t ] || { echo "REFUSED: no role sandbox_schema_apply to retire" >&2; exit 2; }
  # As the admin: borrow the apply role just long enough to hand its objects to the app owner
  # (REASSIGN needs the privileges of both roles), give it back, then no login and no password.
  psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 --single-transaction -q -f - <<'SQL'
SELECT current_user AS admin \gset
GRANT sandbox_schema_apply TO :"admin";
REASSIGN OWNED BY sandbox_schema_apply TO :"admin";
REVOKE sandbox_schema_apply FROM :"admin";
ALTER ROLE sandbox_schema_apply NOLOGIN PASSWORD NULL;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_class WHERE relowner = 'sandbox_schema_apply'::regrole) THEN
    RAISE EXCEPTION 'sandbox_schema_apply still owns relations';
  END IF;
END $$;
SQL
  echo "DONE retire: objects reassigned to the app owner; sandbox_schema_apply is NOLOGIN with no password"
else
  [ "$exists" = t ] || { echo "DONE drop: no role sandbox_schema_apply (nothing to do)"; exit 0; }
  # Recovery: hand anything it still owns to the admin, drop its grants, then the role itself.
  psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 --single-transaction -q -f - <<'SQL'
SELECT current_user AS admin \gset
GRANT sandbox_schema_apply TO :"admin";
REASSIGN OWNED BY sandbox_schema_apply TO :"admin";
DROP OWNED BY sandbox_schema_apply;
REVOKE sandbox_schema_apply FROM :"admin";
DROP ROLE sandbox_schema_apply;
SQL
  echo "DONE drop: sandbox_schema_apply removed"
fi
