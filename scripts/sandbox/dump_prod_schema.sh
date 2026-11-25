#!/usr/bin/env bash
# Prod's SCHEMA (no rows) plus its two migration ledgers, into the sandbox drop. Q15, runbook 10a (prod half).
#
# Runs IN PROD as a gated one-off Cloud Run job (VPC egress to the private Cloud SQL), on an image that has
# pg_dump/psql matching the server's major version, coreutils `timeout` and curl, e.g. FROM postgres:17 +
# curl. The backend image has neither. The sandbox half is jobs/sandbox_schema_apply.py, which re-checks
# everything below with a real tokenizer. The FIRST dump also gets a human read before its upload is
# approved (controller + the owner).
#
# Env:
#   PIVOTA_ENV=production         required: this script runs nowhere else
#   DATABASE_URL                  prod's (from its secret): postgresql://user[:password]@<ip-or-host>[:port]/db[?sslmode=...]
#                                 Never on argv: split into PG* env vars and a 0600 passfile.
#   SANDBOX_SCHEMA_GCS_URI        gs://pivota-sandbox-catalog-drop/<prefix> (the bucket is pinned)
#   STAMP                         optional, YYYYmmddTHHMMZ (default: now, UTC)
#   SCHEMA_DUMP_LOCK_WAIT         optional, default 10s: pg_dump --lock-wait-timeout
#   SCHEMA_DUMP_DEADLINE_SECONDS  optional, default 600: hard cap on each pg_dump / psql call
#   SCHEMA_DUMP_LOCAL_OUT_DIR     tests only: write <dir>/<stamp>/... instead of uploading
#
# PROD IMPACT. Schema-only reads the catalogs and takes ACCESS SHARE on every table (no rows read).
# ACCESS SHARE conflicts only with ACCESS EXCLUSIVE, but a dump WAITING behind a pending DDL lock makes
# every later query queue behind it: --lock-wait-timeout makes it fail fast instead. pg_dump sets its OWN
# session's statement_timeout / lock_timeout / idle_in_transaction_session_timeout to 0 right after
# connecting (measured), so PGOPTIONS below caps psql and pg_dump's first query only; for pg_dump the
# caps are --lock-wait-timeout and the client-side deadline (coreutils `timeout`, required outside
# tests). The session is named sandbox-schema-dump in pg_stat_activity.
#
# WHAT LEAVES PROD: DDL with no owners, privileges, comments, publications, subscriptions, security labels
# or tablespaces; and the ledgers' rows as INSERTs over EXPLICIT columns only (a future ledger column is
# not carried): schema_migrations(id, applied_at) and startup_sql_migrations(filename, checksum, applied_at).
#
# REFUSED (exit 3, nothing uploaded): CREATE SERVER / USER MAPPING / SUBSCRIPTION / PUBLICATION / ROLE /
# USER / GROUP / DATABASE / FOREIGN TABLE / EVENT TRIGGER, ALTER ROLE / USER / SYSTEM / SERVER / DATABASE /
# DEFAULT PRIVILEGES / FOREIGN TABLE, GRANT / REVOKE, SET ROLE / SESSION AUTHORIZATION, DO, row data, psql
# meta-commands but \restrict/\unrestrict, a missing completion trailer, fewer tables than the source,
# and any literal that looks like a secret or a person (email-shaped, sk_/rk_/ak_/pk_/shpat_/ghp_/xox
# prefixes, a 32+ hex run, a 40+ base64 run) anywhere in the schema: function bodies, defaults, CHECKs,
# views, policies.
set -euo pipefail

SANDBOX_DROP_BUCKET="pivota-sandbox-catalog-drop"   # == config.sandbox_identity.SANDBOX_DROP_BUCKET (tested)

die() { echo "dump_prod_schema REFUSED: $*" >&2; exit "${2:-2}"; }

# --- the refusals, shared by the dump and by `check` -----------------------------------------------------
# At COLUMN 0, where pg_dump writes every top-level statement (USER MAPPING and SERVER included). A function
# body's indented INSERT/GRANT is body text, not a statement; a body line at column 0 that starts with one
# is refused too (fail closed, a human reads it). The sandbox half re-checks with a real tokenizer.
FORBIDDEN='^(CREATE[[:space:]]+(SERVER|USER|GROUP|ROLE|DATABASE|SUBSCRIPTION|PUBLICATION|FOREIGN[[:space:]]+TABLE|EVENT[[:space:]]+TRIGGER)|ALTER[[:space:]]+(ROLE|USER|GROUP|SYSTEM|SERVER|DATABASE|DEFAULT[[:space:]]+PRIVILEGES|FOREIGN[[:space:]]+TABLE)|GRANT[[:space:]]|REVOKE[[:space:]]|SET[[:space:]]+(ROLE|SESSION)|DO[[:space:]]|DO$|COPY[[:space:]]|INSERT[[:space:]]|SECURITY[[:space:]]+LABEL)'
SECRETS='[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}|(sk|rk|ak|pk)_(live|test)_[A-Za-z0-9]|shpat_|ghp_|xox[abposr]-|[0-9a-fA-F]{32,}|[A-Za-z0-9+/]{40,}={0,2}'
LEDGER_LINE="^(-- .*|INSERT INTO public\\.schema_migrations \\(id, applied_at\\) VALUES \\('[^';\\\\]*', '[^';\\\\]*'\\);|INSERT INTO public\\.startup_sql_migrations \\(filename, checksum, applied_at\\) VALUES \\('[^';\\\\]*', '[^';\\\\]*', '[^';\\\\]*'\\);)$"

check_files() {  # check_files <schema.sql> <ledgers.sql>; dies with exit 3 on a refusal
  local schema="$1" ledgers="$2" hits meta bad
  tail -5 "$schema" | grep -q 'PostgreSQL database dump complete' || die "schema dump has no completion trailer" 3
  tail -1 "$ledgers" | grep -qx -- '-- sandbox ledger dump complete' || die "ledger dump has no completion trailer" 3
  # Case-insensitive: a hand-written `create user mapping` is as forbidden as pg_dump's upper-case one.
  hits=$( { grep -Eio "$FORBIDDEN" "$schema" || true; } | tr '[:lower:]' '[:upper:]' | tr -s '[:space:]' ' ' | sort -u | tr '\n' ',')
  [ -z "$hits" ] || die "schema dump carries a forbidden statement: $hits" 3
  # Captured, never `| grep -q`: under pipefail an early-exiting grep -q can SIGPIPE the writer and turn a
  # FOUND line into a non-zero pipeline, i.e. a check that silently passes.
  meta=$( { grep -Eh '^\\' "$schema" "$ledgers" || true; } | { grep -Ev '^\\(restrict|unrestrict) [A-Za-z0-9]+$' || true; } )
  [ -z "$meta" ] || die "dump carries a psql meta-command other than \\restrict/\\unrestrict" 3
  # Literal secrets: the kind of the hit only, never the hit.
  # Comments and pg_dump's own \restrict/\unrestrict lines (a random 64-character key) are not scanned.
  hits=$( { grep -Ev '^(--|\\(restrict|unrestrict) )' "$schema" || true; } | { grep -Eo "$SECRETS" || true; } | head -c 1 )
  [ -z "$hits" ] || die "schema dump carries a literal that looks like a secret or a person (email, key prefix, long hex/base64); a human must read it" 3
  bad=$( { grep -Ev "$LEDGER_LINE" "$ledgers" || true; } )
  [ -z "$bad" ] || die "ledger dump carries a line that is not an explicit-column ledger INSERT" 3
}

if [ "${1:-}" = "check" ]; then  # tests and the human read: run only the refusals on two files
  [ "$#" -eq 3 ] || die "usage: dump_prod_schema.sh check <schema.sql> <ledgers.sql>"
  check_files "$2" "$3"
  echo "dump_prod_schema CHECK OK"
  exit 0
fi

# --- environment -----------------------------------------------------------------------------------------
[ "${PIVOTA_ENV:-}" = "production" ] || die "runs only where PIVOTA_ENV=production"
[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is not set"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%MZ)}"
[[ "$STAMP" =~ ^[0-9]{8}T[0-9]{4}Z$ ]] || die "STAMP must look like YYYYmmddTHHMMZ"
LOCK_WAIT="${SCHEMA_DUMP_LOCK_WAIT:-10s}"
DEADLINE="${SCHEMA_DUMP_DEADLINE_SECONDS:-600}"
[[ "$LOCK_WAIT" =~ ^[0-9]+(ms|s)$ ]] || die "SCHEMA_DUMP_LOCK_WAIT must look like 10s or 500ms"
[[ "$DEADLINE" =~ ^[0-9]+$ ]] || die "SCHEMA_DUMP_DEADLINE_SECONDS must be a whole number"
if [ -n "${SCHEMA_DUMP_LOCAL_OUT_DIR:-}" ]; then
  MODE=local
else
  MODE=gcs
  URI="${SANDBOX_SCHEMA_GCS_URI:-}"
  [[ "$URI" =~ ^gs://([a-z0-9._-]+)(/([A-Za-z0-9._/-]*))?$ ]] || die "SANDBOX_SCHEMA_GCS_URI must be gs://$SANDBOX_DROP_BUCKET/<prefix>"
  BUCKET="${BASH_REMATCH[1]}"; PREFIX="${BASH_REMATCH[3]%/}"
  [ "$BUCKET" = "$SANDBOX_DROP_BUCKET" ] || die "SANDBOX_SCHEMA_GCS_URI must name the sandbox drop bucket"
  case "$PREFIX" in *..*) die "SANDBOX_SCHEMA_GCS_URI prefix may not contain '..'" ;; esac
fi
if command -v timeout >/dev/null; then
  capped() { timeout --signal=TERM "$DEADLINE" "$@"; }
elif [ "$MODE" = local ]; then
  capped() { "$@"; }  # tests on a laptop without coreutils
else
  die "coreutils timeout is required: every prod call has a deadline"
fi

WORK="$(mktemp -d)"; chmod 700 "$WORK"; trap 'rm -rf "$WORK"' EXIT

# The DSN never goes on argv: libpq env vars plus a 0600 passfile.
urldecode() { local s="${1//+/ }"; printf '%b' "${s//%/\\x}"; }
DSN="${DATABASE_URL/postgresql+asyncpg:\/\//postgresql://}"
re='^postgres(ql)?://([^:@/?]+)(:([^@/?]*))?@([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(:([0-9]{1,5}))?/([A-Za-z0-9_.-]+)(\?(sslmode=[a-z-]+))?$'
[[ "$DSN" =~ $re ]] || die "DATABASE_URL is not of the form postgresql://user:password@host[:port]/db[?sslmode=...]"
export PGUSER PGHOST PGPORT PGDATABASE PGPASSFILE PGSSLMODE
PGUSER="$(urldecode "${BASH_REMATCH[2]}")"; PGHOST="${BASH_REMATCH[5]}"; PGPORT="${BASH_REMATCH[7]:-5432}"
PGDATABASE="${BASH_REMATCH[8]}"; PGSSLMODE="${BASH_REMATCH[10]#sslmode=}"; PGSSLMODE="${PGSSLMODE:-prefer}"
PGPASSFILE="$WORK/pgpass"
PASSWORD_RAW="${BASH_REMATCH[4]:-}"
( umask 077; esc() { local v="${1//\\/\\\\}"; printf '%s' "${v//:/\\:}"; }
  printf '%s:%s:%s:%s:%s\n' "$(esc "$PGHOST")" "$PGPORT" "$(esc "$PGDATABASE")" "$(esc "$PGUSER")" \
    "$(esc "$(urldecode "$PASSWORD_RAW")")" > "$PGPASSFILE" )
unset DATABASE_URL DSN PASSWORD_RAW
export PGAPPNAME=sandbox-schema-dump
export PGOPTIONS="-c statement_timeout=120s -c idle_in_transaction_session_timeout=60s"
echo "dump_prod_schema: pg_dump $(pg_dump --version | awk '{print $NF}') stamp=$STAMP mode=$MODE lock_wait=$LOCK_WAIT deadline=${DEADLINE}s"

# --- the schema --------------------------------------------------------------------------------------------
capped pg_dump --lock-wait-timeout="$LOCK_WAIT" --schema-only --no-owner --no-privileges --no-comments \
  --no-publications --no-subscriptions --no-security-labels --no-tablespaces --format=plain > "$WORK/schema.sql"

# --- the ledgers, explicit columns, one INSERT per line ------------------------------------------------------
LEDGERS=$(capped psql -XAtc "select coalesce(string_agg(c.relname, ' ' order by c.relname), '') from pg_class c
  join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r'
  and c.relname in ('startup_sql_migrations', 'schema_migrations')")
: > "$WORK/ledgers.sql"
for t in $LEDGERS; do
  case "$t" in
    schema_migrations) q="select format('INSERT INTO public.schema_migrations (id, applied_at) VALUES (%L, %L);', id, applied_at) from public.schema_migrations order by id" ;;
    startup_sql_migrations) q="select format('INSERT INTO public.startup_sql_migrations (filename, checksum, applied_at) VALUES (%L, %L, %L);', filename, checksum, applied_at) from public.startup_sql_migrations order by filename" ;;
    *) die "unexpected ledger $t" ;;
  esac
  capped psql -XAtc "$q" >> "$WORK/ledgers.sql"
done
printf -- '-- sandbox ledger dump complete\n' >> "$WORK/ledgers.sql"

check_files "$WORK/schema.sql" "$WORK/ledgers.sql"

# --- completeness: no fewer tables than the live source -----------------------------------------------------
DUMPED=$(grep -c '^CREATE TABLE ' "$WORK/schema.sql" || true)
LIVE=$(capped psql -XAtc "select count(*) from information_schema.tables where table_schema not in
  ('pg_catalog','information_schema') and table_schema not like 'pg_toast%' and table_type='BASE TABLE'")
[[ "$LIVE" =~ ^[0-9]+$ ]] || die "could not count the source's tables" 3
[ "$DUMPED" -ge "$LIVE" ] || die "schema dump has fewer tables ($DUMPED) than the source ($LIVE)" 3
LEDGER_ROWS=$(grep -c '^INSERT INTO ' "$WORK/ledgers.sql" || true)

gzip -n -9 "$WORK/schema.sql"; gzip -n -9 "$WORK/ledgers.sql"
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | awk '{print $1}'; }
SHA_S=$(sha256 "$WORK/schema.sql.gz")
SHA_L=$(sha256 "$WORK/ledgers.sql.gz")
cat > "$WORK/_MANIFEST.json" <<EOF
{"format_version": 2, "kind": "schema", "stamp": "$STAMP",
 "exported_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
 "pg_dump_version": "$(pg_dump --version | awk '{print $NF}')",
 "tables_in_dump": $DUMPED, "tables_live": $LIVE, "ledger_tables": "$LEDGERS", "ledger_rows": $LEDGER_ROWS,
 "files": {"schema": {"file": "$STAMP/schema.sql.gz", "sha256": "$SHA_S"},
           "ledgers": {"file": "$STAMP/ledgers.sql.gz", "sha256": "$SHA_L"}}}
EOF

put() {  # put <local file> <object name relative to the prefix>
  [[ "$2" =~ ^[A-Za-z0-9._/-]+$ ]] || die "object name $2 has characters outside [A-Za-z0-9._/-]"
  if [ "$MODE" = local ]; then
    mkdir -p "$SCHEMA_DUMP_LOCAL_OUT_DIR/$(dirname "$2")"
    [ ! -e "$SCHEMA_DUMP_LOCAL_OUT_DIR/$2" ] || die "$2 already exists; a dump never overwrites"
    cp "$1" "$SCHEMA_DUMP_LOCAL_OUT_DIR/$2"
    return
  fi
  local name="${PREFIX:+$PREFIX/}$2" code
  # The bearer token goes in a 0600 header file read with -H @file, never on argv.
  ( umask 077; printf 'Authorization: Bearer %s\n' "$(curl -fsS -H 'Metadata-Flavor: Google' \
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
      | sed -E 's/.*"access_token" *: *"([^"]+)".*/\1/')" > "$WORK/auth.h" )
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST --data-binary "@$1" -H @"$WORK/auth.h" \
    -H 'Content-Type: application/octet-stream' \
    "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&ifGenerationMatch=0&name=${name//\//%2F}")
  rm -f "$WORK/auth.h"
  [ "$code" = 200 ] || die "upload of $2 answered HTTP $code" 4
}
put "$WORK/schema.sql.gz" "$STAMP/schema.sql.gz"
put "$WORK/ledgers.sql.gz" "$STAMP/ledgers.sql.gz"
put "$WORK/_MANIFEST.json" "$STAMP/_MANIFEST.json"
echo "dump_prod_schema DONE stamp=$STAMP tables=$DUMPED live=$LIVE ledger_rows=$LEDGER_ROWS"
