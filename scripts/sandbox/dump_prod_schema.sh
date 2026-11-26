#!/usr/bin/env bash
# Prod's SCHEMA (no rows) plus its two migration ledgers, into the sandbox drop. Q15, runbook 10a (prod half).
#
# Runs IN PROD as a gated one-off Cloud Run job (VPC egress to the private Cloud SQL), on an image that has
# pg_dump/psql matching the server's major version and curl, e.g. FROM postgres:17 + curl. The backend
# image has neither. The sandbox half is jobs/sandbox_schema_apply.py, which re-checks everything below.
#
# Env:
#   PIVOTA_ENV=production         required: this script runs nowhere else
#   DATABASE_URL                  prod's (from its secret); never printed
#   SANDBOX_SCHEMA_GCS_URI        gs://<bucket>/<prefix>, e.g. gs://pivota-sandbox-catalog-drop/schema
#   STAMP                         optional, YYYYmmddTHHMMZ (default: now, UTC)
#   SCHEMA_DUMP_LOCAL_OUT_DIR     tests only: write <dir>/<stamp>/... instead of uploading
#
# Writes <prefix>/<stamp>/schema.sql.gz, ledgers.sql.gz, then _MANIFEST.json LAST, each create-only
# (ifGenerationMatch=0: objectCreator is enough, nothing is overwritten, nothing read back).
#
# WHAT LEAVES PROD: DDL only (tables, types, sequences, indexes, constraints, functions, triggers, views,
# extensions), with no owners, privileges, comments, publications, subscriptions, security labels or
# tablespaces; plus the ROWS of exactly two ledgers (migration file ids and timestamps): the backend's
# startup_sql_migrations and the gateway's schema_migrations, when present, so the sandbox's ledgers agree
# with its schema and the gateway's migrate step applies only what prod lacks. The dump is REFUSED
# (exit 3, nothing uploaded) if it carries a statement that can hold a credential or a person:
# CREATE SERVER / USER MAPPING / SUBSCRIPTION / PUBLICATION / ROLE, GRANT / REVOKE, ALTER ROLE / SYSTEM,
# any row data (COPY / INSERT) in the schema file, or a psql meta-command other than \restrict/\unrestrict.
set -euo pipefail

die() { echo "dump_prod_schema REFUSED: $*" >&2; exit "${2:-2}"; }

[ "${PIVOTA_ENV:-}" = "production" ] || die "runs only where PIVOTA_ENV=production"
[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL is not set"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%MZ)}"
[[ "$STAMP" =~ ^[0-9]{8}T[0-9]{4}Z$ ]] || die "STAMP must look like YYYYmmddTHHMMZ"
if [ -n "${SCHEMA_DUMP_LOCAL_OUT_DIR:-}" ]; then
  MODE=local
else
  MODE=gcs
  URI="${SANDBOX_SCHEMA_GCS_URI:-}"
  [[ "$URI" =~ ^gs://([a-z0-9._-]+)(/(.*))?$ ]] || die "SANDBOX_SCHEMA_GCS_URI must be gs://<bucket>/<prefix>"
  BUCKET="${BASH_REMATCH[1]}"; PREFIX="${BASH_REMATCH[3]%/}"
fi
DSN="${DATABASE_URL/postgresql+asyncpg:\/\//postgresql://}"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
echo "dump_prod_schema: pg_dump $(pg_dump --version | awk '{print $NF}') stamp=$STAMP mode=$MODE"

# --- the schema ------------------------------------------------------------------------------------
pg_dump "$DSN" --schema-only --no-owner --no-privileges --no-comments --no-publications \
  --no-subscriptions --no-security-labels --no-tablespaces --format=plain > "$WORK/schema.sql"
tail -5 "$WORK/schema.sql" | grep -q 'PostgreSQL database dump complete' || die "schema dump has no completion trailer" 3

# --- the two ledgers, only those that exist (pg_dump -t on a missing table fails) -------------------
LEDGERS=$(psql "$DSN" -Atc "select string_agg(c.relname, ' ' order by c.relname) from pg_class c
  join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r'
  and c.relname in ('startup_sql_migrations', 'schema_migrations')")
TARGS=()
for t in $LEDGERS; do TARGS+=(-t "public.$t"); done
if [ "${#TARGS[@]}" -gt 0 ]; then
  pg_dump "$DSN" --data-only --column-inserts --no-owner --no-privileges --no-comments --format=plain \
    "${TARGS[@]}" > "$WORK/ledgers.sql"
  tail -5 "$WORK/ledgers.sql" | grep -q 'PostgreSQL database dump complete' || die "ledger dump has no completion trailer" 3
else
  printf -- '-- no ledger tables in the source\n' > "$WORK/ledgers.sql"
fi

# --- refuse anything that can carry a credential or a person ----------------------------------------
FORBIDDEN='^(CREATE (SERVER|USER MAPPING|SUBSCRIPTION|PUBLICATION|ROLE|USER|GROUP|DATABASE)|ALTER (ROLE|USER|GROUP|SYSTEM|SERVER|USER MAPPING|DATABASE)|GRANT |REVOKE |SECURITY LABEL|COPY |INSERT )'
if grep -Eq "$FORBIDDEN" "$WORK/schema.sql"; then
  die "schema dump carries a forbidden statement: $(grep -Eo "$FORBIDDEN" "$WORK/schema.sql" | sort -u | tr '\n' ',')" 3
fi
# Captured, never `| grep -q`: under pipefail an early-exiting grep -q can SIGPIPE the writer and
# turn a FOUND line into a non-zero pipeline, i.e. a check that silently passes.
META=$( { grep -Eh '^\\' "$WORK/schema.sql" "$WORK/ledgers.sql" || true; } | { grep -Ev '^\\(restrict|unrestrict) [A-Za-z0-9]+$' || true; } )
[ -z "$META" ] || die "dump carries a psql meta-command other than \\restrict/\\unrestrict" 3
BAD=$( { grep -Ev '^(--|$|SET |SELECT pg_catalog\.set_config\(|SELECT pg_catalog\.setval\(|\\(restrict|unrestrict) )' "$WORK/ledgers.sql" || true; } \
   | { grep -Ev '^INSERT INTO public\.(startup_sql_migrations|schema_migrations) ' || true; } )
[ -z "$BAD" ] || die "ledger dump carries a statement other than a ledger INSERT" 3

# --- completeness: no fewer tables than the live source ---------------------------------------------
DUMPED=$(grep -c '^CREATE TABLE ' "$WORK/schema.sql" || true)
LIVE=$(psql "$DSN" -Atc "select count(*) from information_schema.tables where table_schema not in
  ('pg_catalog','information_schema') and table_schema not like 'pg_toast%' and table_type='BASE TABLE'")
[ "$DUMPED" -ge "$LIVE" ] || die "schema dump has fewer tables ($DUMPED) than the source ($LIVE)" 3
LEDGER_ROWS=$(grep -c '^INSERT INTO ' "$WORK/ledgers.sql" || true)

gzip -n -9 "$WORK/schema.sql"; gzip -n -9 "$WORK/ledgers.sql"
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | awk '{print $1}'; }
SHA_S=$(sha256 "$WORK/schema.sql.gz")
SHA_L=$(sha256 "$WORK/ledgers.sql.gz")
cat > "$WORK/_MANIFEST.json" <<EOF
{"format_version": 1, "kind": "schema", "stamp": "$STAMP",
 "exported_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
 "pg_dump_version": "$(pg_dump --version | awk '{print $NF}')",
 "tables_in_dump": $DUMPED, "tables_live": $LIVE, "ledger_tables": "$LEDGERS", "ledger_rows": $LEDGER_ROWS,
 "files": {"schema": {"file": "$STAMP/schema.sql.gz", "sha256": "$SHA_S"},
           "ledgers": {"file": "$STAMP/ledgers.sql.gz", "sha256": "$SHA_L"}}}
EOF

put() {  # put <local file> <object name relative to the prefix>
  if [ "$MODE" = local ]; then
    mkdir -p "$SCHEMA_DUMP_LOCAL_OUT_DIR/$(dirname "$2")"
    [ ! -e "$SCHEMA_DUMP_LOCAL_OUT_DIR/$2" ] || die "$2 already exists; a dump never overwrites"
    cp "$1" "$SCHEMA_DUMP_LOCAL_OUT_DIR/$2"
    return
  fi
  local name="${PREFIX:+$PREFIX/}$2" token code
  token=$(curl -fsS -H 'Metadata-Flavor: Google' \
    'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
    | sed -E 's/.*"access_token" *: *"([^"]+)".*/\1/')
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST --data-binary "@$1" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/octet-stream' \
    "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&ifGenerationMatch=0&name=$(printf '%s' "$name" | sed 's#/#%2F#g')")
  [ "$code" = 200 ] || die "upload of $2 answered HTTP $code" 4
}
put "$WORK/schema.sql.gz" "$STAMP/schema.sql.gz"
put "$WORK/ledgers.sql.gz" "$STAMP/ledgers.sql.gz"
put "$WORK/_MANIFEST.json" "$STAMP/_MANIFEST.json"
echo "dump_prod_schema DONE stamp=$STAMP tables=$DUMPED live=$LIVE ledger_rows=$LEDGER_ROWS"
