# ADR-009 A9 — `seller_ref_missing` parity-week watch (runbook)

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** The `railway ssh` / `DATABASE_PUBLIC_URL` steps below read the ROLLBACK database, so
> a parity check run as written measures the wrong platform. They were not translated by guesswork.
> See [operating_on_gcp_production.md](../runbooks/operating_on_gcp_production.md).


**Purpose.** Confirm the seller-of-record seeds backfill made the seller-keyed
closure path total, so the **legacy closure fallbacks can be removed** without
silently dropping attribution. Removal target (a later, separate packet):
- the A9-1 raw-host mismatch compare, and
- the `seller_ref_missing` honest-gap branch
in `services/commerce_attribution_service.close_external_order_conversion`.

**Do not remove those paths until this watch is green** (see exit criteria).

## Context (what already shipped)

- Seeds backfill executed on prod **2026-07-07 ~12:39 UTC**
  (`scripts/backfill_seller_of_record.py --execute --phases seeds`):
  **7,357 external_product_seeds now carry `seller_ref`** (7,307 cross → ~248
  observed sellers; 50 self). **2,026 stay `seller_ref`-NULL** — the honest
  floor (no resolvable destination domain); these are expected to remain NULL.
- T2-1 redirect stamping verified live (signed token carries `seller_ref` +
  `seed_kind`). T2-2/T2-3 closure covered by real-Postgres E2E
  (`tests/test_t2_postgres_integration.py`).

## Daily command

Run once per UTC day. In-container (native internal DB, most reliable):

```
railway ssh --project 9bdca959-cc79-413c-9f23-c8b5396eb5f0 \
  --environment production --service 17b7380b-d0e1-4ff5-8975-516c93cbdc93 \
  -- python -m scripts.parity_watch_seller_ref
```

Operator, via public proxy (fallback):

```
DATABASE_PUBLIC_URL=$(railway variables --service web --kv | sed -n 's/^DATABASE_PUBLIC_URL=//p') \
  python -m scripts.parity_watch_seller_ref \
  >> /tmp/sellerref-parity-$(date -u +%Y%m%d).log
```

Exit code: `0` clean day · `1` drift (a gate is red) · `2` script error.

## Gates (a clean day needs all three)

| gate | metric | clean when |
|---|---|---|
| **A. closure (primary)** | external conversions closed since the anchor with `metadata.seller_ref_missing=true` | `ext_converted_seller_missing_recent == 0` |
| **B. supply (leading)** | recent external-seed clicks whose seed HAS a `seller_ref` but whose token ctx lacks one | `ext_clicks_recent_missing_but_resolvable == 0` |
| **C. write-path (regression)** | seeds inserted since the anchor left `seller_ref`-NULL while resolvable | `seeds_new_resolvable_missing == 0` |

Conversions are currently ~zero, so gate A moves last; gate B (live click
stamping) is the leading indicator and gate C guards the A9-3 new-write path.

Informational (NOT gates — drained by the separate phase-3 catalog re-key,
`--phases catalog`): `catalog_external_seed_bucket` (day-0: 9,456),
`edges_external_seed_bucket` (0), and the `seeds_seller_ref_null_floor`
(day-0: 2,026 — expected steady).

## Exit criteria

Legacy closure paths are safe to remove after **5 consecutive clean days**
(`CLEAN_DAYS_REQUIRED`) — provided at least some real external-seed traffic
(clicks and/or conversions) flowed during the window so the gates were
exercised, not just vacuously green. If a genuine gate goes red, investigate
before the counter resets; do NOT proceed to removal.

## Day-0 baseline (2026-07-07 12:58 UTC)

`verdict: CLEAN` — all gates green (no traffic since backfill yet).
`seller_ref present=7,357 · null_floor=2,026 · conversions=0 · ext_clicks_recent=0`.
