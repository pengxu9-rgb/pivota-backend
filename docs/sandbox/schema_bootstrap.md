# Sandbox schema bootstrap (runbook 10a, Q15)

Decided 25 Nov (the owner: "Dump prod's schema"): the empty sandbox database gets **prod's schema, no rows**,
plus prod's two migration ledgers. Code: `scripts/sandbox/dump_prod_schema.sh` (prod half),
`jobs/sandbox_schema_apply.py` (sandbox half). Tests: `tests/test_sandbox_schema_apply.py`,
`tests/test_sandbox_schema_apply_postgres.py` (real `pg_dump` end to end: the sandbox's
`pg_dump --schema-only` must equal the source's).

## Order on the sandbox database

1. **10a-prod** `dump_prod_schema.sh` in pivota-prod: a gated prod one-off (program + sha review by
   the controller, the owner's go, outside blackout windows).
2. **10a-sandbox** `python -m jobs.sandbox_schema_apply` in pivota-sandbox, once, on the empty DB.
3. **10b** the gateway's `node src/db/cli.js migrate`: with prod's `schema_migrations` rows copied, it
   applies only the gateway migrations prod lacks.
4. **10c** the first `python -m jobs.sandbox_catalog_copy import`.
5. **14a** `register_gateway_service_agent` (dry run, then `--apply` on its own go).

## 10a-prod: `scripts/sandbox/dump_prod_schema.sh`

* Image: `pg_dump`/`psql` of the **server's major version** (prod is Postgres 17), coreutils `timeout`
  and `curl` (`-H @file`), e.g. `FROM postgres:17` + `apt-get install curl` at build time. Cloud Run job
  with VPC egress to the private Cloud SQL, like `infra/gcp/restore_to_cloudsql.sh`.
* Env: `PIVOTA_ENV=production` (refuses otherwise), `DATABASE_URL` from prod's secret in the form
  `postgresql://user:password@<host>[:port]/<db>[?sslmode=...]` (anything else is refused),
  `SANDBOX_SCHEMA_GCS_URI=gs://pivota-sandbox-catalog-drop/schema` (bucket pinned).
* The DSN never reaches argv: it becomes `PGHOST`/`PGPORT`/`PGUSER`/`PGDATABASE`/`PGSSLMODE` and a 0600
  passfile. The bearer token goes in a 0600 header file read by `curl -H @file`.
* **Role (infra Q3):** pg_dump takes ACCESS SHARE on every table it dumps, which needs SELECT on each, so
  the narrow catalog-export role cannot dump the schema. Run this under its own role with SELECT on every
  table (it reads no rows but the two ledgers) and its own secret; keep the catalog export's role narrow.
* Prod impact: `--lock-wait-timeout=10s` (a dump never queues behind a pending ACCESS EXCLUSIVE with prod's
  queries queued behind it), a hard client-side deadline on every call (`SCHEMA_DUMP_DEADLINE_SECONDS`,
  default 600), `PGAPPNAME=sandbox-schema-dump`, `PGOPTIONS` statement/idle timeouts. Measured: pg_dump
  resets its own session's `statement_timeout`, `lock_timeout` and `idle_in_transaction_session_timeout`
  to 0 after connecting, so `PGOPTIONS` caps `psql` and pg_dump's first query only.
* Writes `schema/<stamp>/schema.sql.gz`, `ledgers.sql.gz`, then `_MANIFEST.json` (format 2) last, each
  create-only (`ifGenerationMatch=0`; objectCreator suffices).
* Ledgers: one `INSERT` per row over EXPLICIT columns only, built by `psql format(%L)`:
  `schema_migrations(id, applied_at)`, `startup_sql_migrations(filename, checksum, applied_at)`.
* **Refuses (exit 3, nothing uploaded)**, at column 0 where pg_dump writes top-level statements: CREATE
  SERVER / USER [MAPPING] / GROUP / ROLE / DATABASE / SUBSCRIPTION / PUBLICATION / FOREIGN TABLE / EVENT
  TRIGGER, ALTER ROLE / USER / GROUP / SYSTEM / SERVER / DATABASE / DEFAULT PRIVILEGES / FOREIGN TABLE,
  GRANT, REVOKE, SET ROLE / SESSION, DO, COPY, INSERT, SECURITY LABEL; psql meta-commands but
  `\restrict`/`\unrestrict`; any literal that looks like a secret or a person (email-shaped,
  `sk_/rk_/ak_/pk_` live/test keys, `shpat_`, `ghp_`, `xox?-`, 32+ hex, 40+ base64) outside comments and
  pg_dump's own `\restrict` key; a ledger line that is not an explicit-column INSERT; a missing trailer;
  fewer tables than the live source. `dump_prod_schema.sh check <schema.sql> <ledgers.sql>` runs only these
  checks (tests; the human read of the first dump).
* **The first dump gets a human read before its upload is approved** (controller + the owner).

## 10a-sandbox: `jobs/sandbox_schema_apply.py`

* Env: `PIVOTA_ENV=sandbox` + the full sandbox profile (the startup guard runs), `DATABASE_URL` (every
  address asyncpg would connect to must be in the sandbox's private range, held only as a hash, or exactly
  `/cloudsql/pivota-sandbox:<region>:<instance>/.s.PGSQL.<port>`: `config/sandbox_identity.py`, the single
  source), `SANDBOX_SCHEMA_GCS_URI` (bucket pinned). SA: `objectViewer` on the drop, the DB secret.
* Refuses unless the database is **empty** (no table in any non-system schema).
* Re-checks the manifest (format 2), sha256 per file, and every statement with a real tokenizer (line and
  nested block comments, `'...'` with `''`, `"..."`, `$tag$` bodies, top-level psql meta lines; refuses
  `E'...'`, `U&'...'`, unterminated anything, trailing text without `;`). Each statement's skeleton must be a
  kind pg_dump --schema-only emits (SET, set_config, CREATE SCHEMA/EXTENSION/TYPE/DOMAIN/FUNCTION/PROCEDURE/
  AGGREGATE/SEQUENCE/TABLE/VIEW/INDEX/TRIGGER/RULE/POLICY/..., ALTER TABLE/SEQUENCE/INDEX/TYPE/DOMAIN);
  refused even then: SET ROLE / SESSION, `standard_conforming_strings` other than on, OWNER TO, untrusted
  languages, extensions that connect out or run jobs (dblink, *_fdw, pg_cron, ...), and secret-shaped
  literals. Ledger statements must be exactly the two explicit-column INSERT shapes.
* One transaction (`standard_conforming_strings` forced on): schema, then ledger rows; then the
  base-table count must equal the source's live count and the ledger rows the manifest's, or everything
  rolls back. `search_path` is restored.
* Exit 0 = `DONE {...}`, exit 2 = `REFUSED: <rule>`.

## Residual

* A literal inside DDL (a column DEFAULT, a CHECK, a function body) is copied as written. Nothing in
  the dump can be checked for an embedded secret beyond the statement rules above; read the first
  schema dump's diff against the repo's migrations before the first apply.
