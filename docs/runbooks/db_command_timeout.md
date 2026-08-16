# `DB_COMMAND_TIMEOUT_SECONDS` on production `web`

Follow-up 1 of issue #1759 (out of #1754 / PR #1756).

**Recommendation: set `DB_COMMAND_TIMEOUT_SECONDS=600` on the `web` service,
production environment.** Evidence below. This is a production change and needs
human sign-off before it is applied.

## What the knob does

`db/database.py` reads `DB_COMMAND_TIMEOUT_SECONDS` and, when it is `> 0`,
passes it to asyncpg as `command_timeout` — for the shared `databases.Database`
pool and for the legacy `get_db_pool()` asyncpg pool. It is **per statement**,
not per request and not per scheduler run.

The env var is currently **absent** on `web`/production, so `command_timeout` is
unset. asyncpg has no default statement timeout, and the server has none either
(measured: `statement_timeout = 0`, `idle_in_transaction_session_timeout = 0`,
`lock_timeout = 0` on PostgreSQL 17.10). A socket that dies without RST
therefore leaves an `await` hanging forever — `db/database.py` documents a
36-minute hang of exactly that shape (2026-07-17).

The value is clamped by `_env_float(..., min_value=0.0, max_value=600.0)`, so
**600 is the largest value the code can express.** `0` means unset/infinite.

### Plumbing verified on the production pin

Run in-cluster on `web` (`databases 0.7.0`, `SQLAlchemy 1.4.52`,
`asyncpg 0.31.0` — the actual deployed versions):

```
KWARGS {'min_size': 1, 'max_size': 2, 'timeout': 5.0, 'command_timeout': 1.5}
RAISED builtins.TimeoutError after 1.52 s     # SELECT pg_sleep(6)
REUSE ok: 1                                    # pool healthy afterwards
```

So `databases==0.7.0` does forward the kwarg, the ceiling fires at the value,
the raised type is `builtins.TimeoutError` (an `OSError`/`Exception` subclass —
every `except Exception` site swallows it), and the pooled connection is
returned usable.

## Measurement

Source: `pg_stat_statements` (extension 1.11, enabled and preloaded on the prod
cluster), statistics window **2026-07-11 10:02 UTC → 2026-08-16 15:58 UTC
(36.2 days)**, 4,823 statement fingerprints.

`/__scheduler_health`'s `max_duration_ms` was **not** used to derive the ceiling:
it is per *run* and a run issues many statements, so it is an upper bound only.
It is used below only as a cross-check.

### Statements that ever exceeded 120s

| min | mean | max | calls | statement |
|---:|---:|---:|---:|---|
| 0.0s | 8.7s | **1536.6s** | 238 | `CREATE INDEX IF NOT EXISTS idx_product_evidence_merchant …` |
| 0.0s | 0.1s | **933.2s** | 8,272 | `INSERT INTO content_canonical_election …` |
| 0.0s | 0.0s | **823.3s** | 486,581 | `INSERT INTO index_pipeline_state …` |
| 0.0s | 0.1s | **796.2s** | 9,018 | `INSERT INTO agent_pdp_view …` |
| 0.0s | 4.4s | **665.5s** | 394 | `CREATE INDEX IF NOT EXISTS idx_evidence_items_audit_run …` |
| 320.6s | 320.6s | 320.6s | 1 | `WITH moved AS (… a9_4_backfill_checkpoint …)` |
| 277.7s | 277.7s | 277.7s | 1 | `SELECT count(*) FROM (WITH moved AS (… a9_4_backfill_checkpoint …))` |
| **95.6s** | **124.7s** | **182.0s** | **1,370** | `WITH active_all AS (SELECT * FROM external_product_seeds …)` |
| 69.0s | 105.4s | 152.4s | 1,224 | same query, second parameterisation |
| 128.0s | 128.0s | 128.0s | 1 | `COPY (SELECT … ) TO …` (ad-hoc export) |

`min_exec_time` is the discriminator. A statement whose **min is 0.0s** and whose
mean is single-digit seconds is a fast statement with a lock-wait tail — the
long tail is time spent blocked, not work. A statement whose **min is high** is
genuinely expensive on every call.

The two DDL rows settle it beyond doubt: `product_evidence` holds **1 row
(48 kB)** and `evidence_items` holds **99 rows (216 kB)**. An index build on
those tables is instantaneous; 1536s and 665s are 100% lock wait.

### The slowest *legitimate* statements

1. **Recurring:** `WITH active_all AS (SELECT * FROM external_product_seeds …)`
   — `scripts/mirror_external_seeds_to_catalog_products.py::_build_report`,
   reached from the `external_seed_catalog_materialization` job, which fires
   **every 15 minutes**. min 95.6s / mean 124.7s / **max 182.0s** over 1,370
   calls. Cross-checks against `/__scheduler_health`, which recorded
   `max_duration_ms = 122,566` for that job — one query dominates the run.
   `external_product_seeds` is 12,627 rows / 460 MB and has been effectively
   flat since 2026-07-20 (3 new rows since), so this figure is not growing.

2. **One-off ops:** the `a9_4_backfill_checkpoint` statements at **320.6s** and
   277.7s (`scripts/backfill_seller_of_record.py`, `scripts/verify_seller_rekey.py`
   — not scheduled), and the 128.0s ad-hoc `COPY` export.

3. **Money path is nowhere near the ceiling.** The slowest statement matching
   `transactions|orders|payment|psp_|settlement|invoice|charge` over the whole
   window is **12.65s** max. `payment_reconcile_tick` cannot be cut by any
   candidate value here.

## Why 600

| candidate | fingerprints it would have cut in 36 days | legitimate work cut |
|---:|---:|---|
| 600s | 5 | **none** — all 5 have `min_exec_time = 0.0s`, mean ≤ 8.7s |
| 300s | 6 | the 320.6s A9-4 backfill statement |
| 240s | 7 | + the 277.7s A9-4 statement |
| 180s | 8 | + the 15-minute seed-mirror query (max 182.0s) |
| 120s | 10 | + the 128.0s `COPY`, and most seed-mirror runs |

600s is the only candidate that cuts nothing legitimate, and it is also the
clamp maximum. It clears the slowest recurring statement by 3.3× and the
slowest one-off ops statement by 1.9×.

### What a 600s ceiling would actually have done

- Aborted 5 pathological lock waits (two index builds on near-empty tables,
  three upsert storms) that were each blocked, not working.
- The two DDL sites both swallow the exception and retry:
  `db/product_evidence.py:121` logs a warning and leaves `_DDL_READY` False;
  `db/audit_evidence.py:604` logs at debug per statement. Both use
  `CREATE … IF NOT EXISTS`, so a cut retries harmlessly on the next call. **A
  600s ceiling cannot fail startup.**

### What it does not buy

600s is a **wedge backstop, not a latency budget**. Request-path queries are
bounded far earlier by the HTTP layer; nothing user-facing is protected by a
10-minute ceiling. The value it adds is:

- a cancelled scheduler run on a dead socket stops holding a pool slot
  indefinitely (asyncpg issues its cancel over a *separate* connection, so a
  dead client socket does not block the cancel, and the pool discards the
  connection);
- the request path gains a finite ceiling where it currently has none.

Setting a genuinely tight per-statement budget would first require fixing the
seed-mirror query — see below.

### Operator note

If a future backfill legitimately needs more than 600s in one statement, do
**not** raise the clamp. Run that script with `DB_COMMAND_TIMEOUT_SECONDS=0`
(unset/infinite) for that run. Note that `railway run` injects the service's
variables, so ops scripts launched that way inherit whatever `web` is set to.

## Applying it

Production change — requires human sign-off, and Railway restarts the service
when a variable changes.

```bash
railway variables --set DB_COMMAND_TIMEOUT_SECONDS=600 --project 9bdca959-cc79-413c-9f23-c8b5396eb5f0 --service web --environment production
```

Afterwards, confirm the pool picked it up (in-cluster, read-only):

```bash
railway ssh --project 9bdca959-cc79-413c-9f23-c8b5396eb5f0 --service web --environment production 'python -c "import os;print(os.getenv(\"DB_COMMAND_TIMEOUT_SECONDS\"))"'
```

Rollback is unsetting the variable.

## Separate finding worth its own issue

`external_seed_catalog_materialization` spent **297,134 s ≈ 82.5 hours of
database time in 36.2 days** — about **9.5% of one core, continuously** — on
`_build_report`, a query that takes at least 95.6s *every* run, fires every 15
minutes, and scans a 12,627-row table that stopped growing on 2026-07-20. The
job runs the report to decide whether there is anything to do, and returns early
when `missing_catalog_products == 0`. That preflight should be a cheap existence
check, not a full report build.
