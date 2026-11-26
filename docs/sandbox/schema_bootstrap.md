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
  `\restrict`/`\unrestrict`; a carriage return; any literal that looks like a secret or a person
  (email-shaped; `sk_/rk_/ak_/pk_` live/test keys, `sk-…`, `shpat_`, `ghp_`, `github_pat_`, `xox?-`,
  `whsec_`, `AKIA…`, `AIza…`, JWTs, DSN or `password=` passwords, phone numbers, 32+ hex, 40+ base64)
  outside comments and pg_dump's own `\restrict` key; a ledger line that is not an explicit-column INSERT;
  a missing trailer; fewer tables than the live source; and a pg_dump whose major version is not the
  server's (checked before dumping; the manifest records `pg_dump_version` and `server_version_num`).
* The credential (RUNBOOK 10a-role's read-only `sandbox_schema_dump`) is checked before any pg_dump, exit 3:
  not a superuser and no CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS; a member of NO role (NOINHERIT does
  not stop SET ROLE; pg_write_all_data included); it owns nothing and may not INSERT/UPDATE (any column)/
  DELETE/TRUNCATE any table, view, foreign table or materialized view, nor USAGE/UPDATE a sequence; no
  CREATE on the database or any schema; no EXECUTE on a SECURITY DEFINER function outside pg_catalog;
  owns no function, type, schema, large object or other object (`pg_shdepend`, so ALTER FUNCTION ...
  SECURITY DEFINER is out of reach); may write no large object (an ACL, or `lo_compat_privileges` on); no
  per-role setting but `default_transaction_read_only=on`.
* **Every session is READ ONLY**: `PGOPTIONS` carries `default_transaction_read_only=on`, which outranks the
  role's own default, and the check above refuses a session where `transaction_read_only` is not on. So
  even a credential that slipped past it cannot write, CREATE, or ALTER a role or a function.
  **Accepted trade-off: `lo_create`.** A READ ONLY transaction still allows it (measured on PG15; not yet
  measured on 17), and PUBLIC may EXECUTE it, so any prod role can create a large object. The dump's SQL is
  fixed and never calls it. Closing it is a prod-wide `REVOKE EXECUTE ON FUNCTION lo_create(oid),
  lo_creat(integer), lo_from_bytea(oid, bytea) FROM PUBLIC`, a prod change with its own gate and go,
  not part of this job. The sandbox side counts large objects in its audit (any owner: must be 0).
  NOTE: if prod's `public` schema still grants CREATE to PUBLIC (a pre-15 default carried by a restore),
  this refuses even the right role; the fix is a gated prod REVOKE, never a weaker check.
* These are a COARSE line-based filter. `dump_prod_schema.sh check <schema.sql> <ledgers.sql>` runs only
  them; the authoritative offline check is `python -m jobs.sandbox_schema_apply check` (below).
* **The first dump gets a human read before its upload is approved** (controller + the owner).

## 10a-sandbox: `jobs/sandbox_schema_apply.py`

### The apply role: THE gate (infra bootstrap, as SQL, before the first apply)

A statement-kind check cannot limit what executes at apply time (a materialized view WITH DATA or a
trigger calls a function), so the apply runs as a role that can do nothing but create objects in
`public`. Create it with SQL as the admin, NOT with `gcloud sql users create` (that grants
cloudsqlsuperuser, which the job refuses):

```sql
CREATE ROLE sandbox_schema_apply LOGIN NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB
  NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2 PASSWORD '<from its own secret>';
GRANT CONNECT ON DATABASE <sandbox db> TO sandbox_schema_apply;
-- No TEMP for anyone but who needs it: a temp relation is searched BEFORE pg_catalog, so it could stand in
-- for a catalog the job reads. The job refuses a role that may create temp objects.
REVOKE TEMPORARY ON DATABASE <sandbox db> FROM PUBLIC;
GRANT TEMPORARY ON DATABASE <sandbox db> TO <app role>;  -- jobs/sandbox_catalog_copy.py stages in TEMP tables
GRANT USAGE, CREATE ON SCHEMA public TO sandbox_schema_apply;   -- NOT CREATE ON DATABASE
-- Extensions: the admin pre-creates the fixed list; the dump never creates one.
CREATE EXTENSION IF NOT EXISTS pg_trgm  WITH SCHEMA public;   -- backend migrations
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;   -- backend migrations
CREATE EXTENSION IF NOT EXISTS vector   WITH SCHEMA public;   -- gateway migrations
```

* Schemas: prod is expected to have **only `public`** (no other is known). A dump with another
  `CREATE SCHEMA` fails under this role (no CREATE on the database) and the whole apply rolls back. That
  is a stop for a new decision (controller + the owner), never a wider grant to the role.
* After `DONE`, **as the admin** (never as the apply role, and never by granting it a membership to
  make this work): `REASSIGN OWNED BY sandbox_schema_apply TO <app owner role>;` then
  `ALTER ROLE sandbox_schema_apply NOLOGIN;`.
* The job's own check, before anything runs (`role_refusal`): the connected role is not a superuser,
  cannot create roles or databases, has no REPLICATION or BYPASSRLS, is the session user, is a member of
  NO role, has no CREATE or TEMP on the database, has no per-role setting, and owns nothing yet
  (including large objects; a rerun is refused, never merged into).

### The job

* Env: `PIVOTA_ENV=sandbox` + the full sandbox profile (the startup guard runs), `DATABASE_URL` = the
  APPLY ROLE's DSN from its own secret (every address asyncpg would connect to must be in the sandbox's
  private range, held only as a hash, or exactly `/cloudsql/pivota-sandbox:<region>:<instance>/.s.PGSQL.<port>`:
  `config/sandbox_identity.py`, the single source), `SANDBOX_SCHEMA_GCS_URI` (bucket pinned).
  SA: `objectViewer` on the drop, that one secret.
* Refuses unless: the role passes the check above; the dump's `pg_dump_version` major equals this
  server's; the database is **empty** (no table in any non-system schema, read from `pg_class`); every
  `CREATE EXTENSION` in the dump names an extension the admin already installed.
* Re-checks the manifest (format 2), sha256 per file, and every statement with **PostgreSQL's own
  parser** (`pglast` 7.22 = the PostgreSQL 17 parser, imported lazily by this job only). The allowlist
  is over parse-tree node types with their fields checked: pg_dump's exact preamble SETs only (the
  zero timeouts and `standard_conforming_strings` are checked, never executed); functions in plpgsql or
  sql only; no ownership change of any kind; CREATE SCHEMA without elements or AUTHORIZATION; no CREATE
  TABLE AS; a materialized view only WITH NO DATA; no rule; no trigger on a ledger table; ALTER TABLE
  subcommands from a fixed list; no SECURITY DEFINER function; partition bounds constants only (PostgreSQL
  evaluates a bound at CREATE / ATTACH); no TEMP / TEMPORARY object or `pg_temp` name anywhere; no
  literal that looks like a secret or a person (in the parser's own printout, so no comment).
* **No dump-defined code is reachable while the schema is created** (the 8155964d4 review). In EVERY
  expression position (CHECK, a domain's constraint, DEFAULT, generated column, index / partition-key /
  statistics expression, bound, predicate, policy, trigger WHEN, parameter default) a function or
  operator must be pg_catalog's (an unqualified name is, with `search_path` ''), minus the few that run
  SQL or another type's input (`query_to_xml` and kin, `ts_stat`, `ts_rewrite`, `set_config`, `*_in`/
  `*_recv` of array/record/domain/range); a cast only to a pg_catalog type, an enum the dump creates, or
  a name in the reviewed `EXPRESSION_NAMES_ALLOWED` (EMPTY: neither repo's migrations need one; the first
  real dump's `check` names any prod has, each added only after review that it is an extension's). Since a
  domain's constraints obey the same rule, the input function of every type the dump can create runs
  built-in code only, so even a bare literal coerced into a domain or composite column reaches no
  dump-defined function. Refused outright: CREATE OPERATOR, CREATE CAST, ALTER DOMAIN, CREATE AGGREGATE
  unless reviewed by name (`AGGREGATES_ALLOWED`, empty), a range type with a canonical or subtype_diff
  function, an ALTER TABLE ADD CHECK that is not pg_dump's named NOT VALID form, a partition key column
  of a type that is not pg_catalog's or an enum. Function bodies, view queries, WITH NO DATA
  materialized views and a trigger's function reference are not evaluated when created and are left
  alone. Ledger statements must be exactly the two explicit-column INSERT shapes,
  one row of plain literals each, no quote, semicolon or backslash in a value.
* One transaction with the apply's own `lock_timeout` 10s, `statement_timeout` 120s, idle 60s,
  `standard_conforming_strings` on, `row_security` off, `check_function_bodies` off and `search_path` ''
  (checked unchanged after the schema; then the job's own SQL runs under `search_path = pg_catalog,
  pg_temp` and `row_security` off, checked right before the ledger and again before the audit, and every
  catalog it reads is named `pg_catalog.x`), plus
  CLIENT-side deadlines a statement cannot change: 120s per statement and 900s for the whole apply. Each statement runs ALONE through
  the extended protocol (the server refuses a string with two commands). Then: no ledger table may run
  code on INSERT (trigger, rule, expression/partial index, CHECK/FK, domain or generated column, row
  security, a default other than now()/nextval()); `standard_conforming_strings` re-asserted; the
  ledger rows; then a POST-APPLY AUDIT, the catch-all behind the parser: no rows in any table but the
  ledgers (read with row security off, and past a declared FORCE inside a rolled-back savepoint, so a
  policy can neither hide a row nor run); no GRANT on any table, column, function, type or schema the role
  owns; no default privileges; no SECURITY DEFINER function; no large object at all; no per-role setting
  (`pg_db_role_setting`); no temporary object; no rule but views' `_RETURN`; exactly the dump's triggers
  and policies, and row security ENABLEd / FORCEd on exactly the tables the dump declares; no populated
  materialized view; every sequence as created (`last_value` = its start, not called; `setval(s, n,
  false)` included). Then the base-table count
  must equal the source's live count and the ledger rows the manifest's, or everything rolls back.
  `search_path` is restored. A server newer than the parser's major version (17) is refused.
* Exit 0 = `DONE {...}`, exit 2 = `REFUSED: <rule>`.
* Offline: `python -m jobs.sandbox_schema_apply check <schema.sql> <ledgers.sql>` runs the same parser
  and allowlist with no database (exit 0 = `OK {...}`). **The human read of the first dump runs it**;
  `dump_prod_schema.sh check` is only the coarse line-based filter.

## Residual

* A literal inside DDL (a column DEFAULT, a CHECK, a function body) is copied as written. Nothing in
  the dump can be checked for an embedded secret beyond the statement rules above; read the first
  schema dump's diff against the repo's migrations before the first apply.
