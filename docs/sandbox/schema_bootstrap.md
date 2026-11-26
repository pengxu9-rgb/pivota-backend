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

* Image: `pg_dump`/`psql` of the **server's major version** (prod is Postgres 17) and `curl`, e.g.
  `FROM postgres:17` + `apt-get install curl` at build time. The backend image has neither
  (`libpq5` only). Cloud Run job with VPC egress to the private Cloud SQL, like
  `infra/gcp/restore_to_cloudsql.sh`.
* Env: `PIVOTA_ENV=production` (refuses otherwise), `DATABASE_URL` from prod's secret,
  `SANDBOX_SCHEMA_GCS_URI=gs://pivota-sandbox-catalog-drop/schema`.
* SA: needs only `storage.objectCreator` on the drop bucket (uploads are `ifGenerationMatch=0`, no
  list, no read) and the DB secret. The export SA (`sa-sandbox-export@pivota-prod`) fits.
* Writes `schema/<stamp>/schema.sql.gz`, `ledgers.sql.gz`, then `_MANIFEST.json` last.
* `pg_dump --schema-only --no-owner --no-privileges --no-comments --no-publications
  --no-subscriptions --no-security-labels --no-tablespaces`; ledgers by
  `--data-only --column-inserts -t public.startup_sql_migrations -t public.schema_migrations`
  (whichever exist).
* **Refuses (exit 3, nothing uploaded)** a dump carrying `CREATE SERVER / USER MAPPING /
  SUBSCRIPTION / PUBLICATION / ROLE / DATABASE`, `ALTER ROLE / SYSTEM / SERVER`, `GRANT / REVOKE`,
  `COPY / INSERT` (rows) in the schema file, any psql meta-command but `\restrict`/`\unrestrict`, or
  a ledger statement that is not a ledger INSERT. A user mapping holds an FDW password; that is
  the case this exists for (tested with a real postgres_fdw mapping). Also refuses a missing
  completion trailer or fewer dumped tables than the live source.
* Checks run at column 0, where pg_dump writes top-level statements. A function body that starts a
  line with `INSERT`/`GRANT` at column 0 would be refused too (fail closed); the sandbox half's
  check ignores function bodies.

## 10a-sandbox: `jobs/sandbox_schema_apply.py`

* Env: `PIVOTA_ENV=sandbox` + the full sandbox profile (the startup guard runs), `DATABASE_URL`
  (must be in the sandbox's private range, held only as a hash, or the `pivota-sandbox` Cloud SQL socket:
  `config.sandbox_identity.sandbox_database_refusal`, the single source for project and range),
  `SANDBOX_SCHEMA_GCS_URI`. SA: `objectViewer` on the drop, the DB secret.
* Refuses unless the database is **empty** (no table in any non-system schema).
* Re-checks the manifest, sha256 per file, and the same statement rules, ignoring dollar-quoted
  function bodies and string literals; strips `\restrict`/`\unrestrict`.
* One transaction: schema, then ledger rows; then the base-table count must equal the source's live
  count and the ledger rows the manifest's, or everything rolls back. `search_path` is restored.
* Exit 0 = `DONE {...}`, exit 2 = `REFUSED: <rule>`.

## Residual

* A literal inside DDL (a column DEFAULT, a CHECK, a function body) is copied as written. Nothing in
  the dump can be checked for an embedded secret beyond the statement rules above; read the first
  schema dump's diff against the repo's migrations before the first apply.
