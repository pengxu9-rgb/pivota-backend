# Stage 1 — Shadow-Mode Rollout

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** The `railway ...` commands below have NOT been rewritten — they were left as-is
> rather than translated by guesswork, because the procedures here were never re-verified against
> GCP. Running one changes the platform nobody is served from: the incident continues while the
> dial reads as turned. Translate with
> [operating_on_gcp_production.md](../../runbooks/operating_on_gcp_production.md) before acting, or treat this
> document as a historical record of how the Railway rollout was done.


Stage 1 turns on the parts of v1.3 that observe and aggregate, but not the parts that move money. Runs against real shopping-agent and creator-agent traffic for a minimum of 3 days before promoting to Stage 2 (the historical-edge backfill).

Pre-req: Stage 0 complete. Production on commit ≥ `73d4631`. Migration 121 verified applied.

## 0. Prerequisite — register T6 + T5 crons

**Code change required before Stage 1 can be considered started.** Currently `services/audit_scheduler.py` does NOT register any v1.3 cron jobs. Without registration, T6 GMV aggregation never runs and the "GMV rollup output" monitoring item below has nothing to look at.

The fix is a small edit to `services/audit_scheduler.py` adding two jobs. **Outside the scope of this round (no-code-changes briefing rule). Surfaced to Cowork — see `questions_for_cowork.md` v1.3 Stage 1 prerequisite.**

Required additions (illustrative; exact insertion point follows the existing `scheduler.add_job(...)` pattern):

```python
# T6 GMV aggregation — daily at 02:00 UTC, processes yesterday's edges
scheduler.add_job(
    _run_gmv_aggregation_yesterday,
    "cron", hour=2, minute=0, id="gmv_aggregation_daily",
    misfire_grace_time=900, coalesce=True, max_instances=1,
)
# T5 reservation reaper — every 5 minutes
scheduler.add_job(
    services.metering_service.expire_stale_reservations,
    "interval", minutes=5, id="metering_expire_reservations",
    misfire_grace_time=60, coalesce=True, max_instances=1,
)
```

Without these jobs, Stage 1's monitoring step "gmv_daily rollup output" reads zero rows every day and exit criteria can't be met.

## 1. Definition of shadow mode

Status table — exact state during Stage 1:

| Component | State | Reason |
|-----------|-------|--------|
| T9 attribution stamping (`services/psp_payment_finalizer.py:228`) | **ON** | Already inline on every `finalize_payment_success` call after Stage 0. Stamps real agent-driven orders with `gross_attributed_gmv_cents = max(subtotal − discount, 0) × 100`. IS NULL idempotency guard means repeat fires are no-ops. |
| T5 metering service | **OFF in flow / ON as reaper** | No production code path currently calls `reserve()` / `commit()` — no agent dispatcher invokes T5 yet. The 5-min reaper from §0 runs but finds nothing to expire. Safe. |
| T6 GMV aggregation cron | **ON** (after §0 prereq lands) | Daily 02:00 UTC. Populates `gmv_attribution_daily` from `commerce_attribution_edges`. No money moves. |
| T7 invoice generation cron | **OFF** | Not registered. `services/invoice_generation_service.py::run_billing_cycle` is callable as a service function but no scheduler entry triggers it. Code dormant. |
| T8 partner settlement cron | **OFF** | Same — service exists, no cron. |
| `/api/billing/checkout-session` route | **ON** | Reachable. Real Stripe Live Customers + Subscriptions can be created. **But no merchant should call this in Stage 1** — promotion to Stage 3 gates first live billing on a single merchant under $1,000 cap. |
| `/webhooks/stripe/billing` route | **ON, registered with Stripe Live** | Endpoint live, signing secret `STRIPE_BILLING_WEBHOOK_SECRET` set. No events will fire because no Live subscriptions exist. Test-mode endpoint stays active for existing flows. |
| Existing commerce webhook `/webhooks/stripe` | **UNCHANGED** | Continues serving Live commerce webhooks. v1.3 deploy did not touch this. |

## 2. Activation sequence

In order. Each step has a verification.

### 2.1 Confirm T9 is firing on new orders

T9 turned on automatically at Stage 0 deploy. Confirm by reading edges created after the Stage 0 deploy timestamp:

```sql
SELECT COUNT(*) AS edges_after_stage_0,
       COUNT(*) FILTER (WHERE gross_attributed_gmv_cents IS NOT NULL) AS stamped,
       MIN(created_at) AS oldest, MAX(created_at) AS newest
FROM commerce_attribution_edges
WHERE created_at > '<STAGE_0_DEPLOY_TIMESTAMP_UTC>';
```

Expected: `stamped` should equal `edges_after_stage_0` (all new edges stamped). If unstamped > 0 on new edges, T9 is not firing on that order path — investigate per §5 first-failure-mode.

### 2.2 Land §0 prerequisite (T6 cron + T5 reaper registrations)

**AUTHORIZATION REQUIRED.** Cowork lands the `services/audit_scheduler.py` change in a small PR, merges, Railway auto-redeploys.

Verify cron registration after redeploy:

```bash
railway logs --deployment --service web --environment production | grep -i "gmv_aggregation_daily\|metering_expire_reservations" | head -10
```

Expected: APScheduler init log shows both jobs added with their triggers.

### 2.3 First T6 run (manual, ahead of next 02:00 UTC tick)

**AUTHORIZATION REQUIRED.** Trigger one-shot for yesterday's date to seed `gmv_attribution_daily`:

```bash
railway ssh --project 9bdca959-cc79-413c-9f23-c8b5396eb5f0 --environment production --service web \
  "python3 -c \"
import asyncio
from datetime import date, timedelta
from services.gmv_aggregation_service import aggregate_daily
n = asyncio.run(aggregate_daily(date.today() - timedelta(days=1)))
print(f'rollup rows: {n}')
\""
```

Verify:
```sql
SELECT COUNT(*) AS rollups_today, SUM(net_attributed_gmv_cents) AS total_net_cents
FROM gmv_attribution_daily
WHERE date = CURRENT_DATE - INTERVAL '1 day';
```

Expected: rollups_today ≥ 0 (depends on whether any agent orders landed yesterday). Stage 1 doesn't require a non-zero number; it requires that the query returns and matches §3 sanity check.

### 2.4 Capture DB stamp-update timing baseline (after first real stamp)

`pg_stat_statements.mean_exec_time` for the T9 stamp UPDATE is meaningless until that UPDATE has fired at least once. Stage 0 §6 smoke tests are read-only; Stage 1 must capture the baseline once 1–2 live agent orders have stamped real edges.

Wait for the first live agent order to land after Stage 0 deploy:
```sql
SELECT edge_id, order_id, gross_attributed_gmv_cents, updated_at
FROM commerce_attribution_edges
WHERE gross_attributed_gmv_cents IS NOT NULL
  AND updated_at > '<STAGE_0_DEPLOY_TIMESTAMP_UTC>'
ORDER BY updated_at ASC LIMIT 5;
```

When ≥ 1 row returns (first stamp landed), capture the baseline using the query in Appendix A §A.5. Record `stamp_update_baseline_ms` somewhere shared (questions_for_cowork.md or the deployment record) — every subsequent daily comparison in §3.5 references it.

If pg_stat_statements is not installed or returns no row, install it first (`CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` — **AUTHORIZATION REQUIRED**) and let one more stamp fire before retrying baseline capture.

## 3. What to monitor during shadow mode

Six checks, run daily. Full SQL for each is in **Appendix A** (one block per check, copy-paste-able). Stage 1→2 promotion needs 3 consecutive clean days across all six.

| # | Check | Expected | Fail signals |
|---|-------|----------|--------------|
| 3.1 | Daily agent-order count == stamped-edge count (yesterday) | counts match exactly | T9 missed an order path → §5 |
| 3.2 | GMV math: `cae.gross_attributed_gmv_cents == (subtotal − discount) × 100` for each new edge | 0 mismatched rows | T9 included tax/shipping or refund mishandled → §5 |
| 3.3 | `gmv_attribution_daily` rollups reconcile to raw edges via the COALESCE join | 0 mismatched rows | T6 aggregation drifted OR upsert missed via the expression-index join key (migration 110) |
| 3.4 | Error log scan on `psp_payment_finalizer` + `gmv_aggregation_service` | no error/exception/traceback lines | Any hit = Stage 2 blocker |
| 3.5 | DB stamp UPDATE p99 timing vs Stage 0 baseline | `current_ms ≤ 1.5 × baseline` | Lock contention; check `pg_locks` |
| 3.6 | True duplicate edges — same (order_id, merchant_id, agent_id, channel_partner_id) tuple | 0 rows | Concurrent stamping race; legitimate multi-edge-per-order fan-out (one per surface_click_event) is NOT a duplicate |

## 4. Duration and exit criteria

Minimum 3 consecutive UTC days of clean shadow mode.

Exit criteria (all must be true):
- §3.1 returns identical counts every day → T9 stamps every agent order
- §3.2 returns 0 rows every day → GMV math is exact
- §3.3 returns 0 rows every day → T6 rollup matches edges by hand
- §3.4 returns no error lines → handlers + aggregator are quiet
- §3.5 stays within 1.5× baseline → no DB perf regression
- ≥ 5 stamped edges from live agent traffic accumulated over the 3 days → path actually exercises, not just sits idle

Stage 1 → Stage 2 promotion is gated on the §6 checklist below.

## 5. Failure-mode runbook

Partial Symptom → Investigation. Full incident response uses the existing `docs/monetization/runbooks/*` set.

### Symptom: edges not getting stamped (§3.1 counts diverge)

Investigation:
1. Confirm `services/psp_payment_finalizer.py:228` (`await stamp_gross_attributed_gmv(...)`) is reached:
   ```bash
   railway logs --service web --environment production | grep "stamp_gross_attributed_gmv" | tail -20
   ```
2. If reached: check the UPDATE returned 0 rows because the edge already had a non-NULL value (T9's IS NULL guard). Drill on a specific order:
   ```sql
   SELECT edge_id, order_id, gross_attributed_gmv_cents, created_at, updated_at
   FROM commerce_attribution_edges
   WHERE order_id = '<one_of_the_missing>';
   ```
3. If `stamp_gross_attributed_gmv` is NOT reached for that order: the payment finalizer call path didn't reach the hook. Re-read T9 deliverable in PR #581 commit chain. Possible regression: a payment path bypasses `finalize_payment_success` (e.g., an admin manual mark-paid).
4. If the order has NO edge at all in `commerce_attribution_edges`: the commerce-attribution writer (separate workstream, predates v1.3) didn't create an edge. T9 can only stamp existing edges. Not a v1.3 bug.

### Symptom: GMV math doesn't match (§3.2 returns rows)

Investigation:
1. Pick one diverging order, inspect:
   ```sql
   SELECT order_id, subtotal, discount_total, tax, shipping_fee, total
   FROM orders WHERE order_id = '<diverging>';

   SELECT edge_id, gross_attributed_gmv_cents, refund_amount_cents
   FROM commerce_attribution_edges WHERE order_id = '<diverging>';
   ```
2. Recompute by hand: `expected = max(subtotal - discount_total, 0) * 100`. Compare to `gross_attributed_gmv_cents`.
3. If `gross > expected`: tax or shipping was included by mistake. Bug in T9's `_gross_attributed_gmv_cents` helper. v1.3 §1.3 is explicit — neither tax nor shipping.
4. If `gross < expected` AND `refund_amount_cents = 0`: a refund landed without `apply_refund(edge_id, ...)` being called. Bug in the refund pipeline integration — `services/refund_service.py` should call `apply_refund` for any refund on an attributed order.

## 6. Stage 1 → Stage 2 promotion checklist

All must be ✓ before kicking off Stage 2 (the historical-edge backfill).

- [ ] §0 prerequisite landed: T6 cron + T5 reaper registered in `services/audit_scheduler.py`.
- [ ] 3 consecutive UTC days with §3.1–§3.6 all returning clean results.
- [ ] ≥ 5 stamped attribution edges accumulated from live agent traffic.
- [ ] DB stamp-update p99 timing within 1.5× of Stage 0 baseline.
- [ ] No errors in `psp_payment_finalizer` or `gmv_aggregation_service` logs.
- [ ] No unexpected cron failures: `railway logs --service web --environment production | grep "gmv_aggregation_daily" | tail -7` shows successful daily completion for the last 3 ticks.
- [ ] One-shot dry run of `aggregate_daily` for a future date returns 0 rows (proves the merchant_id=NULL cron path works post-Bug-C fix from commit `ba78b0b`).
- [ ] **Ultrareview findings reconciled.** `/ultrareview` was launched against PR #581 (core v1.3) in parallel with Stage 1; findings live in `docs/monetization/CODE_REVIEW_FINDINGS_v1.3.md`. Promotion gate:
  - **Critical-severity findings affecting stamping math, idempotency, money flow, or partner balance accounting** → fix with a scoped v1.3.x PR + redeploy; Stage 1 monitoring window restarts.
  - **Critical-severity findings affecting only paused code paths (T7/T8) or read-only diagnostic paths** → fix as v1.3.x patch; does NOT restart the Stage 1 window because Stage 1 doesn't exercise those paths. Stage 4 promotion gate (not here) re-checks.
  - **Medium / low-severity findings** → log into `CODE_REVIEW_FINDINGS_v1.3.md` with a disposition (fix-now / fix-in-v1.4 / wontfix-with-rationale); they do not block Stage 2 promotion.
  - **No ultrareview run completed** → run it before promoting. The 3-day monitoring window is necessary but not sufficient on its own.
- [ ] Cowork sign-off on the 3-day report.

When all ticked, Stage 2 prompt drafts and the alpha's 91 historical edges get backfilled retroactively. That stage will be briefed separately.

---

## Appendix A — Daily shadow-mode monitoring queries

Run each block daily. Capture results in a shared log; promotion to Stage 2 (§6) requires 3 consecutive UTC days clean across all six.

### A.1 Daily agent-order count vs stamped edges (§3.1)

```sql
WITH agent_orders AS (
  SELECT order_id, subtotal, discount_total, created_at
  FROM orders
  WHERE payment_status = 'paid'
    AND agent_id IS NOT NULL
    AND created_at >= CURRENT_DATE - INTERVAL '1 day'
    AND created_at <  CURRENT_DATE
),
stamped_edges AS (
  SELECT cae.order_id, cae.gross_attributed_gmv_cents
  FROM commerce_attribution_edges cae
  JOIN agent_orders o ON o.order_id = cae.order_id
)
SELECT (SELECT COUNT(*) FROM agent_orders)                                     AS agent_orders_yesterday,
       (SELECT COUNT(*) FROM stamped_edges)                                    AS edges_yesterday,
       (SELECT COUNT(*) FROM stamped_edges WHERE gross_attributed_gmv_cents IS NOT NULL) AS edges_with_value;
```

Expected: all three columns equal.

### A.2 GMV math reconciliation (§3.2)

```sql
SELECT
  o.order_id,
  CAST(GREATEST(o.subtotal - COALESCE(o.discount_total, 0), 0) * 100 AS BIGINT) AS expected_cents,
  cae.gross_attributed_gmv_cents AS stamped_cents,
  CAST(GREATEST(o.subtotal - COALESCE(o.discount_total, 0), 0) * 100 AS BIGINT) - cae.gross_attributed_gmv_cents AS delta_cents
FROM orders o
JOIN commerce_attribution_edges cae ON cae.order_id = o.order_id
WHERE o.payment_status = 'paid'
  AND o.agent_id IS NOT NULL
  AND o.created_at >= CURRENT_DATE - INTERVAL '1 day'
  AND o.created_at <  CURRENT_DATE
  AND ABS(CAST(GREATEST(o.subtotal - COALESCE(o.discount_total, 0), 0) * 100 AS BIGINT) - COALESCE(cae.gross_attributed_gmv_cents, 0)) > 0;
```

Expected: 0 rows. v1.3 §1.3: `GMV = subtotal − discount_total`, tax/shipping excluded.

### A.3 gmv_attribution_daily ↔ edge sums reconcile (§3.3)

```sql
WITH edge_sums AS (
  SELECT DATE(cae.created_at) AS d, cae.merchant_id, cae.agent_id, cae.channel_partner_id,
         SUM(cae.gross_attributed_gmv_cents) AS gross_sum,
         SUM(COALESCE(cae.refund_amount_cents, 0)) AS refund_sum
  FROM commerce_attribution_edges cae
  WHERE DATE(cae.created_at) = CURRENT_DATE - INTERVAL '1 day'
    AND cae.gross_attributed_gmv_cents IS NOT NULL
  GROUP BY DATE(cae.created_at), cae.merchant_id, cae.agent_id, cae.channel_partner_id
)
SELECT e.d, e.merchant_id, e.agent_id, e.channel_partner_id,
       e.gross_sum, gad.gross_attributed_gmv_cents,
       e.refund_sum, gad.refund_amount_cents,
       (e.gross_sum - gad.gross_attributed_gmv_cents) AS gross_delta
FROM edge_sums e
LEFT JOIN gmv_attribution_daily gad
       ON gad.date = e.d
      AND gad.merchant_id = e.merchant_id
      AND COALESCE(gad.agent_id, '') = COALESCE(e.agent_id, '')
      AND COALESCE(gad.channel_partner_id, -1) = COALESCE(e.channel_partner_id, -1)
WHERE gad.id IS NULL OR e.gross_sum <> gad.gross_attributed_gmv_cents OR e.refund_sum <> gad.refund_amount_cents;
```

Expected: 0 rows. COALESCE join MUST match migration 110's expression index.

### A.4 Error log scan (§3.4)

```bash
railway logs --deployment --service web --environment production 2>&1 \
  | grep -iE "psp_payment_finalizer|gmv_aggregation_service|stamp_gross_attributed" \
  | grep -iE "error|exception|traceback" \
  | head -50
```

Expected: no output.

### A.5 DB stamp-update timing (§3.5)

Capture baseline once during Stage 1 §2.4 (after the first live agent order stamps in Stage 1 — pg_stat_statements has nothing to report until then; Stage 0 smoke is read-only so no UPDATE fires):
```sql
SELECT now() AS captured_at,
       (SELECT COUNT(*) FROM commerce_attribution_edges) AS total_edges,
       (SELECT pg_size_pretty(pg_relation_size('commerce_attribution_edges'))) AS edge_table_size,
       (SELECT mean_exec_time FROM pg_stat_statements
         WHERE query LIKE '%UPDATE commerce_attribution_edges%gross_attributed_gmv_cents%'
           AND query LIKE '%IS NULL%'
         ORDER BY calls DESC LIMIT 1) AS stamp_update_baseline_ms;
```

Daily comparison query:
```sql
SELECT mean_exec_time AS current_ms, calls, rows
FROM pg_stat_statements
WHERE query LIKE '%UPDATE commerce_attribution_edges%gross_attributed_gmv_cents%'
  AND query LIKE '%IS NULL%'
ORDER BY calls DESC LIMIT 1;
```

Expected: `current_ms ≤ 1.5 × baseline_ms`.

### A.6 Concurrent stamping race — true duplicates only (§3.6)

v1.3 permits multiple attribution edges per order (one per `surface_click_event` — explicit in T9 acceptance criteria). A true race-bug duplicate is two edges with **identical attribution dimensions** for the same order, not just the same order_id.

```sql
SELECT order_id, merchant_id, agent_id, channel_partner_id,
       COUNT(*) AS dup_count, ARRAY_AGG(edge_id) AS edges
FROM commerce_attribution_edges
WHERE DATE(created_at) = CURRENT_DATE - INTERVAL '1 day'
GROUP BY order_id, merchant_id, agent_id, channel_partner_id
HAVING COUNT(*) > 1;
```

Expected: 0 rows. Multi-edge fan-out across distinct (merchant/agent/partner) tuples is legitimate and not flagged.
