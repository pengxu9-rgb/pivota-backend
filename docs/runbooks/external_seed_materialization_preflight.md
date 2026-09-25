# external_seed_catalog_materialization — preflight cost

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** The `railway ...` commands below have NOT been rewritten — they were left as-is
> rather than translated by guesswork, because the procedures here were never re-verified against
> GCP. Running one changes the platform nobody is served from: the incident continues while the
> dial reads as turned. Translate with
> [operating_on_gcp_production.md](operating_on_gcp_production.md) before acting, or treat this
> document as a historical record of how the Railway rollout was done.


Found while measuring for issue #1759 follow-up 1 (`DB_COMMAND_TIMEOUT_SECONDS`).

## What was wrong

`run_external_seed_catalog_materialization_tick()` fires every 15 minutes. It
called `_build_mirror_report(sample_limit=0, ...)` as a **preflight**, purely to
read one integer — `totals.missing_catalog_products` — and returned early when
that was 0. Nothing else in the tick consumed the report. On a tick that *did*
have work it paid for a second full build afterwards (the `after` report).

The report is expensive for a reason that has nothing to do with the count.
`COMMON_CTES.ranked` does `SELECT eps.*` plus a dozen `seed_data #>>`
extractions per row, so Postgres detoasts the whole JSONB column and
materializes it through a sort. `external_product_seeds` is only 12,627 rows and
its main heap is ~25 MB — but its TOAST segment is ~207 MB, and none of it is
needed to answer "does any active seed still lack a catalog mirror?".

Measured on production 2026-08-17 from `pg_stat_statements`
(36.2-day window, 2026-07-11 → 2026-08-16), two fingerprints of that chain:

| calls | min | mean | max | total |
| ---: | ---: | ---: | ---: | ---: |
| 1,371 | 95.6s | 124.6s | 182.0s | 170,883s |
| 1,224 | 69.0s | 105.4s | 152.4s | 128,952s |

~83 hours of database time — about 9.5% of one core, continuously. Note `min`:
the query never once completed in under 69 seconds. The seed table has been
effectively flat since 2026-07-20 (3 new rows), so this was near-pure waste.

## The fix

`scripts/mirror_external_seeds_to_catalog_products.MISSING_MIRROR_CTES` derives
the same `missing` set without touching `seed_data`, exposed as
`count_missing_catalog_mirrors()`. The tick uses that plus the (cheap)
`_required_schema()` check and builds nothing else; the mirror script's write
contract and idempotency are untouched — `_apply()` is unchanged.

`GET /admin/catalog-products/invariants` was the other reader paying full price
for the same count, and now uses the same helper.

Measured in-cluster against production, same DB, same `databases` handle:

```
schema.ok = True                         (0.017s)
missing_catalog_products = 0             (0.129s)
external_seed mirrors with sig = 0       (0.004s)
FULL NEW QUIET-TICK COST = 0.146s        (was ~125s)
```

### Why the count is exact rather than `LIMIT 1`

The `DISTINCT ON` sort over the seed rows dominates and cannot be
short-circuited, so bounding saves nothing measurable (0.15s exact vs 0.11s for
`LIMIT 1`) while costing operators an honest number in the job's log line.

### Equivalence

The two chains must not drift — the tick now trusts the cheap one to decide
whether to do any work at all, so a disagreement is silently lost mirroring.
They share `_WINNER_ORDER_BY` and `_CANDIDATE_FILTERS` by construction, and
`tests/test_missing_mirror_count_equivalence_postgres.py` pins the rest.

Checked against the report chain on live production data before shipping:

* the winning seed row is identical for all 11,352 groups;
* the `missing` sets are identical live (0 = 0) and with the identity join
  forced to miss (9 = 9, which does exercise the attached-backlink anti-join).

**Do not over-trust that production run.** Measured the same day, production has
0 duplicate `external_product_id` groups, 0 over-length ids, 0 blank titles, 0
NULL/uppercase/padded statuses and 0 lowercase markets — so every group is a
singleton and it could not exercise the winner ranking (`DISTINCT ON` vs
`row_number() = 1`) or any candidate filter at all. It proves the anti-joins on
real data and nothing more. The fixture test is the real proof of the rest: it
constructs the duplicate groups and dirty values production happens to lack.

## Verifying after deploy

The window is cumulative since `stats_reset`, so compare **calls**, not totals —
the historical seconds stay on the counter. The fingerprints above should stop
advancing; `calls` freezes at whatever it read at deploy time.

```bash
railway ssh --project 9bdca959-cc79-413c-9f23-c8b5396eb5f0 --service web --environment production
```

Then, in-cluster:

```sql
SELECT queryid, calls,
       round((total_exec_time/1000.0)::numeric)::bigint AS total_s,
       round((mean_exec_time/1000.0)::numeric, 1) AS mean_s
FROM pg_stat_statements
WHERE query LIKE '%active_standalone%'
ORDER BY total_exec_time DESC;
```

Expect: `calls` on the two big fingerprints unchanged across two readings ≥30
minutes apart (≥2 ticks). If they are still climbing, the deploy did not take or
something else calls `_build_report` on a schedule.

A tick that finds work still builds nothing heavy, but `_apply()` itself runs
the full `COMMON_CTES` chain to fetch the rows it inserts — that is expected and
is bounded by `EXTERNAL_SEED_MATERIALIZATION_BATCH_SIZE`. Seeing those
fingerprints advance *while seeds are actually being added* is correct.
