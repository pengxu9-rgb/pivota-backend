# Stage 2 — Historical Edge Backfill

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** The `railway ...` commands below have NOT been rewritten — they were left as-is
> rather than translated by guesswork, because the procedures here were never re-verified against
> GCP. Running one changes the platform nobody is served from: the incident continues while the
> dial reads as turned. Translate with
> [operating_on_gcp_production.md](../../runbooks/operating_on_gcp_production.md) before acting, or treat this
> document as a historical record of how the Railway rollout was done.


**Status:** DRAFT. Not executable until Stage 1 §6 promotion checklist passes + the architectural decisions in §1 are signed off by Cowork.

Stage 1 turns on T9 stamping for *new* orders. Stage 2 retroactively creates `commerce_attribution_edges` rows for paid orders that predate T9 — so T6's daily rollup and downstream T7 invoice math can include the alpha merchant's full history.

Stage 2 does NOT move money. T7/T8 remain paused. The output of Stage 2 is rows in `commerce_attribution_edges` and a recomputed `gmv_attribution_daily`. Real billing starts in Stage 3.

## 0. Current state (2026-05-22)

Verified against production via the public Postgres proxy:

| Cohort | Order count | Gross cents | Action |
|---|---|---|---|
| Real agent (`agent_982b1ea2df866206`) | 80 | 290,644 | **Backfill candidates** |
| `ops_canary` (Wix writeback canaries) | 10 | 25,319 | **Skip** — operational test traffic, not real attribution |
| NULL `agent_id` (direct/organic) | 8 | 13,800 | **Design decision** — see §1 |
| Already-stamped (Mar 30-31, surface=`ucp`) | 7 | 17,500 | Untouched |
| **Total paid orders for alpha** | **98** | — | — |
| **Total agent-attributable to backfill** | **80** | **290,644** | — |

Real backfill scope is **80 orders, ~$2,906 GMV**, not the 91 mentioned in the Stage 1 runbook. The runbook's "91 historical edges" number predated this audit; it counted ops_canary + NULL-agent rows that don't represent real attribution.

## 1. Open design decisions (need Cowork sign-off before §3 executes)

### 1.1 NULL-agent / direct orders — backfill or skip?

8 paid orders have `orders.agent_id IS NULL` and no Pivota click metadata. They are direct or organic checkout traffic.

**Option A (recommended): skip.** Stage 2 only backfills orders with concrete agent attribution. The 8 orders stay unstamped; they don't appear in T6 rollup; T7 doesn't bill on them. Aligns with the agent-routed monetization model — `has_attribution_signal()` would have rejected them at write time anyway.

**Option B: backfill as direct.** Insert edges with `surface='direct'`, `agent_id=NULL`, `channel_partner_id=NULL`. T6 groups them under `(date, merchant, NULL, NULL)` — a "merchant direct" bucket. T7 would then bill the take rate on this bucket, attributing GMV to *no one* on the agent side. Whether that's desired depends on whether Pivota's take rate applies to non-agent traffic. **The blueprint v1.3 doesn't address this directly.**

### 1.2 Surface tag for backfilled edges

Backfilled edges need a `surface` value. T6 groups by `(date, merchant, agent_id, channel_partner_id)` so `surface` doesn't affect billing math, but it's visible downstream for analytics and disputes.

**Option A (recommended): `historical_backfill_v1.3.2`.** Distinguishes backfilled rows from live T9-stamped rows (`ucp`, `agent`, etc.). Anyone querying for "real-time agent attribution" can filter this surface out.

**Option B: reconstruct from `orders.agent_session_id`.** Some session IDs encode the surface (e.g. `agent_982b..._<epoch>`). Could derive `surface='agent'` for those. But it's an inference, not a record — risky.

### 1.3 `click_id` scheme

Every `commerce_attribution_edges` row needs a `click_id`. Live stamping uses a generated `clk_<uuid hex>`; backfilled orders have no real click to point to.

**Option A (recommended):** `clk_backfill_<sha8(order_id)>`. Deterministic — same input order_id always produces the same click_id. Lets the backfill script be safely re-runnable and matches Stage 0's idempotency philosophy.

**Option B:** generate fresh `clk_*` UUIDs per backfill run. Loses idempotency; re-running the script duplicates edges unless ON CONFLICT logic catches it.

### 1.4 `channel_partner_id` — leave NULL

Recommend NULL for all backfilled edges. No prior session captured partner attribution at order time; reconstructing it would be guesswork. T7 + T8 settle on `channel_partner_id IS NULL` as the "Pivota direct" bucket, which is the correct semantics.

### 1.5 Idempotency strategy

The backfill script MUST be re-runnable without producing duplicates. Two enforcement layers:

1. **Deterministic `edge_id`** derived from `(merchant_id, order_id)` so a replay generates the same primary key. Matches the existing pattern in `services/commerce_attribution_service.py:upsert_order_attribution_edge` (`f"cae_{uuid.uuid5(uuid.NAMESPACE_URL, f'{merchant_id}:{order_id}').hex[:24]}"`).
2. **`ON CONFLICT (edge_id) DO NOTHING`** on the INSERT. Belt-and-suspenders.

The script should print "skipped (already exists)" for every replay row so ops can see the dedup at work.

## 2. Prerequisites

- [ ] Stage 1 → Stage 2 promotion checklist in `STAGE_1_SHADOW_MODE_ROLLOUT.md` §6 — ALL ticked.
- [ ] Cowork sign-off on §1 design decisions above.
- [ ] DB backup taken (retained per Stage 0 §3: until Stage 3 → Stage 4 promotion).
- [ ] `scripts/stage2_backfill_attribution_edges.py` reviewed; dry-run executed against staging if possible.

## 3. Execution sequence

### 3.1 Dry-run on staging (or read-only against production)

```bash
DATABASE_PUBLIC_URL=$(railway variables --json -e staging -s Postgres-xMr6 \
  | jq -r '.DATABASE_PUBLIC_URL')
python3 scripts/stage2_backfill_attribution_edges.py \
  --merchant-id merch_efbc46b4619cfbdf \
  --dry-run
```

Expected output: list of order_ids that would be backfilled (80 lines), each with the synthesized edge_id, surface, click_id, and gross_cents. No writes.

### 3.2 Sanity-check the dry-run output

Run the §A.2 GMV math reconciliation query (from STAGE_1 Appendix A) against the dry-run's stated values. Every line's `gross_cents` must equal `ROUND((subtotal - discount_total) * 100)`. If any mismatch, halt — there's a calculation bug to fix before write.

### 3.3 Authorization gate

**AUTHORIZATION REQUIRED.** This step writes new rows to production. Cowork explicit per-action OK.

### 3.4 Live backfill

```bash
DATABASE_PUBLIC_URL=$(railway variables --json -e production -s Postgres-xMr6 \
  | jq -r '.DATABASE_PUBLIC_URL')
python3 scripts/stage2_backfill_attribution_edges.py \
  --merchant-id merch_efbc46b4619cfbdf \
  --commit
```

Script writes rows inside a transaction; on any error, the whole batch rolls back.

### 3.5 Post-backfill verification

Run the same baseline queries Stage 1 §A captured:

| Check | Expected |
|---|---|
| Edge count for alpha | 7 (existing `ucp`) + 80 (new backfill) = 87 |
| §A.1 historical-period agent_orders == stamped_edges | match per day |
| §A.2 GMV math mismatch_rows | 0 |
| Idempotency: re-run with `--commit` | "skipped (already exists)" on all 80 rows |

### 3.6 Trigger T6 recompute for affected dates

```bash
python3 -c "
import asyncio
from datetime import date
from services.gmv_aggregation_service import recompute_for_date
# Affected date range: 2025-11-24 to 2026-04-23 (from §0 table)
# Iterate each calendar day in range. For efficiency, only days with >=1
# backfilled edge per the script's stdout.
asyncio.run(recompute_for_date(date(2025,12,1), 'merch_efbc46b4619cfbdf'))
# ... repeat for each affected date ...
"
```

Or use a one-liner cursor loop. The Stage 2 script can optionally emit a `--recompute-after` flag that does this automatically.

### 3.7 §A.3 rollup reconciliation

After all recomputes finish, §A.3 (the rollup ↔ edge sum reconciliation query from STAGE_1 Appendix A) must return zero drift rows across the backfill date range.

## 4. Exit criteria

- [ ] 80 edges inserted (or whatever §1.1 decides for the NULL-agent cohort)
- [ ] §A.2 GMV math: 0 mismatches across the backfilled rows
- [ ] §A.3 rollup reconciliation: 0 drift in `gmv_attribution_daily` for all affected dates
- [ ] Idempotency proven: a second `--commit` run inserts 0 new rows
- [ ] Trail log entry appended to `docs/monetization/questions_for_cowork.md` with row counts, GMV totals, and surfaced design decisions

## 5. Rollback

The Stage 2 backfill is reversible because every backfilled edge carries `surface='historical_backfill_v1.3.2'`:

```sql
BEGIN;
DELETE FROM commerce_attribution_edges
WHERE merchant_id = 'merch_efbc46b4619cfbdf'
  AND surface = 'historical_backfill_v1.3.2';
-- Verify count matches the original backfill count before COMMIT
ROLLBACK;  -- or COMMIT after verification
```

T6 rollup must then be recomputed for the same date range to drop the rolled-up cents.

## 6. Stage 2 → Stage 3 promotion

Stage 3 is the **first small-volume live-money flow**. Stage 2 → Stage 3 promotion is briefed separately (`STAGE_3_*.md`, TBD). The Stage 2 → 3 prerequisites at a minimum include:

- [ ] Stage 2 §4 exit criteria all ✓
- [ ] POSTGRES_PASSWORD rotated (leaked value still active from the 2026-05-22 incident)
- [ ] `pg_stat_statements` activated via Postgres restart (gates Stage 1 §3.5 timing baseline; needed for Stage 3 perf monitoring)
- [ ] Cowork sign-off on the Stage 2 trail log entry

---

## Appendix A — Backfill query (dry-run preview)

```sql
SELECT o.order_id,
       o.subtotal,
       o.discount_total,
       ROUND((COALESCE(o.subtotal,0) - COALESCE(o.discount_total,0)) * 100)::BIGINT AS gross_cents,
       o.agent_id,
       o.agent_session_id,
       o.created_at::date AS order_date,
       'cae_' || SUBSTRING(
         MD5(o.merchant_id || ':' || o.order_id) FROM 1 FOR 24
       ) AS would_be_edge_id,
       'clk_backfill_' || SUBSTRING(MD5(o.order_id) FROM 1 FOR 8) AS would_be_click_id,
       'historical_backfill_v1.3.2' AS would_be_surface
FROM orders o
WHERE o.merchant_id = :merchant_id
  AND o.payment_status = 'paid'
  AND o.agent_id IS NOT NULL
  AND o.agent_id NOT IN ('ops_canary')        -- §0 cohort filter
  AND NOT EXISTS (
    SELECT 1 FROM commerce_attribution_edges cae
    WHERE cae.order_id = o.order_id
  )
ORDER BY o.created_at;
```

Production count as of 2026-05-22: 80 rows.

(`uuid5(NAMESPACE_URL, ...)` from the live Python writer doesn't have a clean SQL equivalent, so the runbook preview uses `MD5` for visualization. The actual script computes `uuid5` to match the live writer's edge_id scheme.)
