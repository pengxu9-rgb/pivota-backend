# First boot against an empty database (GCP / Cloud Run)

What happens when this service starts against a database where **nothing
exists yet** — the case Railway never exercises, because there every relation
is already present and all the startup DDL is a no-op.

Measured 2026-08-19 on Cloud Run `pivota-staging` (fresh Cloud SQL PG17) and
reproduced locally on Postgres 15.

## First boot on a PRODUCTION deployment needs one env var

`main.startup()` returns early when `_should_skip_heavy_startup_init()` is
true, which — with `SKIP_HEAVY_STARTUP_INIT` unset — is **whenever the
platform resolves to production** (`config/platform.is_production()`). That
early return sits *before* the `db/migrations/*.sql` runner and before the
`webhook_events` re-check, so a production revision against a **fresh** database
creates only the SQLAlchemy metadata tables and the light schema guard: no
migrations, no `webhook_events`, no ACP tables.

That gate is deliberate (it protects healthcheck timing on an established
database) and this change does not alter it. But on a genuinely empty database
it means the schema is never built, so the **first** boot against a new
production database must run with:

```
SKIP_HEAVY_STARTUP_INIT=false
```

and can be set back afterwards — the `startup_sql_migrations` ledger makes later
boots cheap anyway. The advisory lock still protects `startup_event()`'s DDL
in either mode.

## Symptoms this replaced

```
asyncpg.exceptions.UniqueViolationError: duplicate key value violates unique
constraint "pg_type_typname_nsp_index"
  DETAIL: Key (typname, typnamespace)=(agent_webhook_configs, 2200) already exists.
  main.py:629 startup_event -> services/agent_webhook_service.py:235
services.webhook_service: relation "webhook_events" does not exist
db/migrations 002_production_tables.sql: syntax error at or near "ON" ... INDEX idx_error_type (error_type)
db/migrations 007_agents_management_upgrade.sql: column "status" does not exist
```

Locally, two concurrent boots against one empty database crashed **3 of 6**
processes with the pg_catalog unique violation before the fix, and **0 of 6**
after.

## The four causes

1. **`CREATE TABLE IF NOT EXISTS` is not atomic across sessions.** Two
   sessions creating the same relation both pass the existence check; the
   loser gets a unique violation on `pg_type_typname_nsp_index` /
   `pg_class_relname_nsp_index`. Concurrency sources: Cloud Run starting more
   than one instance, a rolling revision overlapping the old one, and
   background workers that call the lazy `ensure_*_table()` helpers.

2. **A migration runner that could not apply 6 of the 219 files.**
   `main.startup()` sent each `db/migrations/*.sql` file whole through
   `conn.execute(text(body))` on an AUTOCOMMIT connection. psycopg2 sends a
   multi-statement string as ONE implicit transaction, so the 4 files whose
   statements use `CONCURRENTLY` (`051`, `059`, `132`, `135`) always failed
   with *"CREATE INDEX CONCURRENTLY cannot run inside a transaction block"*;
   and `text()` parses `:name` as a bind parameter **even inside a `--`
   comment**, killing `191_acp_checkout_sessions` and
   `192_acp_delegate_allowances` (both describe a cache key
   `acp_complete:<session_id>:a<attempt>` in prose). A file is all-or-nothing,
   so the `ALTER TABLE`s that shared those files never landed either.

   An earlier form of that runner called `conn.commit()` on a legacy
   SQLAlchemy 1.4 `Connection`, which has no such method — every file raised
   `AttributeError` and only files beginning with a DDL keyword survived, via
   1.4 legacy autocommit. That is where `relation "webhook_events" does not
   exist` originally came from. It is fixed on `main`; this runner keeps it
   fixed by using a real transaction per file.

3. **Two migrations that can never apply on Postgres** (see below).

4. **A latched schema probe.** `startup_event()` probes `webhook_events`
   *before* `startup()` applies the migration that creates it, and the probe
   memoized "not available" for the whole process — webhook audit persistence
   and idempotency stayed off until the next restart.

## What the service does now

* `db/startup_ddl.py` — `StartupDdlLock` / `startup_ddl_lock()` holds a
  **session-scoped** advisory lock on key `0x5049565400000001`, taken on a
  dedicated raw asyncpg connection, for the whole startup DDL phase in both
  `startup_event()` and `startup()`. Concurrent instances serialize; the
  connection is closed on release, so the lock cannot leak.
  * Deliberately **not** `pg_advisory_xact_lock`: an open transaction in the
    same database makes `CREATE INDEX CONCURRENTLY` (migrations 051, 059) wait
    forever. That wedged a local boot for 10+ minutes.
  * Deliberately **polled** (`pg_try_advisory_lock` with 0.05s→1s backoff)
    rather than blocking in `pg_advisory_lock`: a blocking acquire keeps a
    statement — and its transaction snapshot — open on the *waiter*, and the
    holder's `CREATE INDEX CONCURRENTLY` waits for exactly that. Two boots
    then wait on each other until one times out. Same 10+ minute wedge,
    different mechanism.
  * `STARTUP_DDL_LOCK_TIMEOUT_SECONDS` (default **600**) bounds acquisition.
    A cold migration run is the thing being waited on, so this must exceed it;
    at 120s the waiter gave up, ran the same migrations unlocked, and the two
    boots deadlocked on `135_*` and `180_*`. On timeout or any lock error the
    boot continues **unlocked** and logs `continuing UNLOCKED` — a lock
    problem must degrade boot, never block it.
* `db/startup_ddl.execute_ddl()` classifies "someone else created it first"
  (SQLSTATE 42P07/42710/42701/42P06/42723, and 23505 **only** on a pg_catalog
  unique index) as success. The `ensure_*_tables()` helpers use it, so a racer
  that is not under the lock still boots. A unique violation on a *user* table
  is still a real error.
* `db/sql_migrations.py` — one transaction per file with a real commit, a
  single engine for the whole run, files containing `CONCURRENTLY` executed
  statement-by-statement on an AUTOCOMMIT connection (psycopg2 sends a
  multi-statement string as one implicit transaction, so AUTOCOMMIT alone is
  not enough), and a `startup_sql_migrations` ledger (`filename`, `checksum`,
  `applied_at`) written in the same transaction as the file, so each file
  applies at most once per database and a failing file is retried next boot.
  Editing an already-applied file logs a checksum warning and does **not**
  re-run it — add a new migration instead.
  * Bodies run on the **raw DBAPI cursor**, not `text()` and not
    `exec_driver_sql()`. `text()` reads `:name` as a bind parameter even
    inside a `--` comment (`191_acp_checkout_sessions`,
    `192_acp_delegate_allowances` died on a comment describing the cache key
    `acp_complete:<session_id>:a<attempt>`); `exec_driver_sql()` passes
    psycopg2 an empty parameter mapping, so every file containing a literal
    `%` failed with "dict is not a sequence" — 30+ files.

Cost on a warm database: one advisory-lock round trip, one
`CREATE TABLE IF NOT EXISTS` and one `SELECT ... FROM startup_sql_migrations`.

## What the first boot on staging will actually do

Under the runner on `main` today, **6** of the 219 files fail and roll back
completely: `191_acp_checkout_sessions` and `192_acp_delegate_allowances`
(a `:token` in a comment parsed as a bind parameter) and the four files whose
statements use `CONCURRENTLY` — `051_external_seed_text_trgm_concurrent`,
`059_catalog_pivot_search_indexes`, `132_catalog_offer_suppression_writer_audit`,
`135_catalog_product_sku_stale_suppression`. Because each file is one
transaction, the `ALTER TABLE`s in 132/135 never landed either.

So the first boot after this change applies those six for the first time,
inside the lifespan — and uvicorn binds the listening socket only after
lifespan startup completes. That includes **11 `CREATE INDEX CONCURRENTLY`**
statements, several of them GIN trigram indexes on the catalog tables. On a
production-sized catalog that is minutes, not seconds, with nothing answering
`/health`.

Before deploying this to an environment with a large catalog, either:

* check which of those objects already exist (`\d+ catalog_products`,
  `select indexname from pg_indexes where tablename in ('catalog_products','catalog_skus','external_product_seeds')`)
  and pre-seed the ledger for the files that are already applied:
  `INSERT INTO startup_sql_migrations (filename, checksum) VALUES ('132_catalog_offer_suppression_writer_audit.sql', 'preseeded') ON CONFLICT DO NOTHING;`
* or apply those files by hand first (they are safe to run outside a
  transaction) and then pre-seed.

On an empty database none of this matters — the indexes build instantly.

## The ledger is NOT called `schema_migrations`

Production already has a `schema_migrations` table, and it belongs to a
different system: 71 rows keyed `id` (`001_taxonomy.sql`,
`004_look_replicator.sql`, …), columns `id` + `applied_at`, nothing in this
repo reads or writes it.

The first cut of this runner used that name. `CREATE TABLE IF NOT EXISTS`
then no-op'd against the foreign table, and every ledger `INSERT` failed
*inside the migration's own transaction* — so **all 219 files rolled back**,
each logged as a routine "may be already applied" warning. Reproduced
2026-08-20 against a copy of the production shape.

Three things now stand between that and a repeat, because on a shared database
the name alone is not enough:

1. The ledger is `startup_sql_migrations`.
2. The ledger row is written **after** the migration's transaction commits,
   never inside it. A ledger that is unwritable for any reason — a view, a
   missing unique constraint for `ON CONFLICT`, an extra `NOT NULL` column, no
   `INSERT` grant — can then cost at most a re-run, never a rollback.
3. If a relation of our name exists and the runner cannot read `filename,
   checksum` out of it, it **refuses to run migrations at all** and logs an
   error.

Point 3 deserves its reasoning, because "apply everything anyway" sounds like
the safer fallback and is not. Without the ledger the runner cannot tell what
already ran, and re-running the tree against a populated database re-executes
convergent writes. Measured over three boots in that mode:

* `126_subscription_plans_allowance_rebase.sql` sets
  `subscription_plans.monthly_credit_allowance` back to 4000/18000/75000 —
  an allowance changed through the app is silently reverted at the next boot.
* `139_*` and `146_*` re-tombstone / re-deactivate catalog rows an operator
  may have revived.
* `013_consolidate_routing.sql` appends a `routing_migration_log` row per boot.
* `003`, `024` and `027` cannot be re-run at all (`CREATE TRIGGER` has no
  `IF NOT EXISTS` in PG15), so from the second boot on they fail forever and
  the summary reads permanently red — indistinguishable from a real outage.

If you pre-seed, pre-seed `startup_sql_migrations`. Leave `schema_migrations`
alone.

## Disabled migrations

Renaming a file to `*.sql.disabled` is how a migration is retired; the runner
globs `*.sql` only. Each disabled file carries a header saying why.

| file | why it can never apply | why not "just fix it" |
|---|---|---|
| `002_production_tables.sql` | MySQL dialect — inline `INDEX idx_error_type (error_type)`, `ON UPDATE CURRENT_TIMESTAMP` | its `agent_metrics (agent_id PRIMARY KEY, ...)` conflicts with the live shape from `009_agent_observability.sql`; its other tables are referenced by no code |
| `007_agents_management_upgrade.sql` | `CREATE INDEX ... ON agents(status)` — the SQLAlchemy `agents` table has `is_active`, not `status`; the views also want `a.name`/`a.email`/`a.last_active` | its Part 2 would create the **wrong** `agent_metrics` before `009` runs, silently breaking `services/agent_metrics_collector.py` on every fresh database |

## Known-failing migrations on a clean database (unchanged by this work)

After the runner fix a fresh database applies **215 of 219** files. The
remaining four fail for reasons in the migration *content*, not the runner:

| file | error | cause |
|---|---|---|
| `012b_routing_extensions.sql` | `trigger "set_routing_resolved_by" ... already exists` | the file's body is duplicated — it creates the trigger twice |
| `014_dual_sided_revenue.sql` | `relation "agent_revenue_policies" does not exist` | duplicated body: the second copy re-runs `ALTER TABLE agent_revenue_policies RENAME TO agent_revenue_expectations` |
| `015_agent_portal_settlement.sql` | `relation "agent_revenue_expectations" does not exist` | depends on the rename in 014 |
| `017_agent_payout_comprehensive.sql` | `numeric field overflow` | a seed value exceeds its `DECIMAL` precision |

Consequence on a fresh database: the agent payout/settlement tables
(`agent_revenue_expectations`, `agent_payout_settings`, `payout_transactions`,
…) do not exist. De-duplicating those files is a separate change — they have
never applied on any environment through this runner, so nothing regressed.

## Verifying locally

```bash
createdb -h 127.0.0.1 -p 55433 -U postgres -E UTF8 -T template0 --locale=C cleanboot
PIVOTA_PG_TEST_URL=postgresql://postgres@127.0.0.1:55433/postgres \
  python -m pytest tests/test_startup_ddl_race_postgres.py tests/test_sql_migrations_runner_postgres.py -q
```

Both gate files are named `tests/test_*_postgres.py` so
`.github/workflows/postgres-dialect-gate.yml` discovers them automatically,
runs them against a real Postgres, and fails the job on any skip. They read
`DATABASE_URL` (which that job sets) and accept `PIVOTA_PG_TEST_URL` as a
local override.

Measured on a fresh database with this branch: two concurrent boots finish in
2.1s and 3.1s, 215/219 migrations applied, 257 tables, `webhook_events`
present; a warm boot is 1.3s and applies nothing.

Without `PIVOTA_PG_TEST_URL` the Postgres-backed cases skip and only the unit
gates run.
