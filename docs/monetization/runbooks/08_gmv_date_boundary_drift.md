# 08 — GMV aggregation date-boundary drift

**Authorization:** read-only investigation; resolution requires recomputing rollups (UPSERT, safe).

## Symptom

`gmv_attribution_daily` totals don't match raw `commerce_attribution_edges`. Operator sees off-by-one-day misattribution: a sale that closed at `2026-05-01 23:50 Asia/Shanghai` shows up under `2026-05-01` in one view and `2026-04-30` in another. Or a brand's monthly GMV sums to a different number depending on which side of the join you start from.

## Investigation

1. Confirm the divergence is timezone-related, not a missing edge:
   ```sql
   SELECT date, SUM(gross_attributed_gmv_cents) AS daily_gross
   FROM gmv_attribution_daily
   WHERE merchant_id = '<merchant_id>'
     AND date BETWEEN <period_start> AND <period_end>
   GROUP BY date ORDER BY date;
   ```
   vs.
   ```sql
   SELECT DATE(e.created_at AT TIME ZONE 'UTC') AS edge_utc_date,
          SUM(e.gross_attributed_gmv_cents) AS edge_gross
   FROM commerce_attribution_edges e
   WHERE e.merchant_id = '<merchant_id>'
     AND e.created_at BETWEEN <period_start> AND <period_end>
     AND e.gross_attributed_gmv_cents IS NOT NULL
   GROUP BY edge_utc_date ORDER BY edge_utc_date;
   ```
2. T6 aggregates by `DATE(e.created_at)` which uses the Postgres session timezone. Confirm the staging/production session TZ:
   ```sql
   SHOW timezone;
   ```
   Expected: `UTC`. If anything else, drift is guaranteed when comparing to `gmv_attribution_daily.date` (which is `DATE` type, no timezone).
3. Spot-check a few edges around the day boundary:
   ```sql
   SELECT edge_id, created_at, created_at AT TIME ZONE 'UTC' AS utc_ts,
          gross_attributed_gmv_cents
   FROM commerce_attribution_edges
   WHERE merchant_id = '<merchant_id>'
     AND created_at::date IN ('<date-1>', '<date>')
   ORDER BY created_at;
   ```

## Resolution

- **Session timezone is not UTC:** fix the session/server timezone first. `ALTER SYSTEM SET timezone = 'UTC';` then reload. Re-run aggregation for affected dates:
  ```python
  # Authorization required for admin REPL access.
  from services.gmv_aggregation_service import recompute_for_date
  for day in affected_dates:
      await recompute_for_date(day, merchant_id='<merchant_id>')
  ```
  `recompute_for_date` is UPSERT — safe to re-run, will overwrite the wrong totals.
- **Edge with a NULL `gross_attributed_gmv_cents`:** T9 didn't stamp the edge (likely the order's `mark_paid` happened before the stamping hook landed). Fix via the stamping helper:
  ```python
  # services.psp_payment_finalizer.stamp_gross_attributed_gmv(...)
  # Then recompute_for_date for the affected merchant + date.
  ```
- **Refund that landed without `apply_refund` being called:** call `apply_refund(edge_id, refund_amount_cents)` and then `recompute_for_date` will flow through.

## Prevention

- Staging and production session timezone must be UTC. Lock this in Railway env: `PGTZ=UTC` on the Postgres service.
- Migration 110 (`gmv_attribution_daily.uq_gmv_attribution_daily_rollup`) uses `COALESCE(agent_id, '')`, `COALESCE(channel_partner_id, -1)` for the UPSERT key — confirm `ON CONFLICT` in `services/gmv_aggregation_service.py` matches exactly. (Already verified by `test_gmv_aggregation_service.py`.)
- Add a daily reconciliation query that compares `SUM(daily.gross)` to `SUM(edges.gross WHERE created_at::date = daily.date)`. Alert on > 1¢ delta.
