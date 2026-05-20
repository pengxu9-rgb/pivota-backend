# Codex prompt — T6: Draft services/gmv_aggregation_service.py (gross/refund/net rollup)

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Stack: Python (FastAPI), Postgres (Railway), Stripe (already integrated as PSP).
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` — implement v1.3 exactly, do not improvise on architecture.
Existing patterns to follow: see db/, services/, routes/, adapters/ — match style of existing files.
Output: code in the existing repo layout. Migrations as SQL files in db/migrations/.
Don't add new dependencies unless absolutely required. Don't rewrite existing code unless explicitly asked.

## Prerequisite inputs — read these first

1. `docs/monetization/T2_db_audit.md` — section 2 for `orders` and `commerce_attribution_edges` column names; section 7 extension points for `commerce_attribution_edges`.
2. `db/migrations/109_extend_commerce_attribution_edges_monetization.sql` — the new billing columns on `commerce_attribution_edges`, including `gross_attributed_gmv_cents`, `refund_amount_cents`, `net_attributed_gmv_cents` (GENERATED ALWAYS AS STORED), `take_rate_applied_bp`, `channel_partner_id`, `refunded_at`.
3. `db/migrations/110_gmv_attribution_daily.sql` — the `gmv_attribution_daily` rollup table you write to.
4. `db/migrations/103_extend_merchants_monetization.sql` — `merchants.promo_period_until` and `merchants.current_tier` columns you read for take-rate logic.
5. Read `db/orders.py` for the `orders` table column names — specifically `subtotal`, `discount_total`, `total`, `tax`, `shipping_fee`.

## Critical constraint — read carefully before writing any SQL

**Migration 076 defines the UPSERT idempotency key as an expression index, not a column-list unique constraint:**

```sql
CREATE UNIQUE INDEX uq_gmv_attribution_daily_rollup
  ON gmv_attribution_daily (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1));
```

**Your `ON CONFLICT` clause in every UPSERT into `gmv_attribution_daily` MUST match this expression exactly:**

```sql
ON CONFLICT (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))
DO UPDATE SET ...
```

A standard `ON CONFLICT (date, merchant_id, agent_id, channel_partner_id)` will NOT work when `agent_id` or `channel_partner_id` is NULL — Postgres treats NULLs as distinct in column-list unique constraints, so the ON CONFLICT would never match NULL-keyed rows and would insert duplicates instead of upserting. Use the COALESCE expression form.

## GMV definition (v1.3 §1.3 lock-in — exact)

```
GMV = orders.subtotal - orders.discount_total
```

Excludes: `orders.tax`, `orders.shipping_fee`, `orders.total` (do not use `total` for GMV). These exclusions are explicit in v1.3 and must not change.

## Take rate (v1.3 §1.3)

- Promo rate: **500 basis points (5%)** — applies when `merchants.promo_period_until IS NOT NULL AND merchants.promo_period_until > NOW()`.
- Standard rate: **1000 basis points (10%)** — all other cases.
- `take_amount_cents = net_attributed_gmv_cents * take_rate_bp / 10000` (integer arithmetic; truncate, do not round).

## Task

Create `services/gmv_aggregation_service.py` with the following functions:

### `async def aggregate_daily(date: date) -> int`

Returns the number of rollup rows written/updated.

Aggregate `commerce_attribution_edges` for the given date into `gmv_attribution_daily`.

```
SELECT
    DATE(e.created_at) AS date,
    e.merchant_id,
    e.agent_id,
    e.channel_partner_id,
    SUM(e.gross_attributed_gmv_cents) AS gross_sum,
    SUM(e.refund_amount_cents) AS refund_sum
FROM commerce_attribution_edges e
WHERE DATE(e.created_at) = :date
  AND e.gross_attributed_gmv_cents IS NOT NULL
GROUP BY DATE(e.created_at), e.merchant_id, e.agent_id, e.channel_partner_id

FOR EACH row in results:
    net = max(gross_sum - refund_sum, 0)
    look up merchant promo_period_until → take_rate_bp = 500 or 1000
    take_amount = net * take_rate_bp // 10000

    UPSERT into gmv_attribution_daily:
    INSERT INTO gmv_attribution_daily
      (date, merchant_id, agent_id, channel_partner_id,
       gross_attributed_gmv_cents, refund_amount_cents, net_attributed_gmv_cents,
       take_rate_bp, take_amount_cents, updated_at)
    VALUES (...)
    ON CONFLICT (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))
    DO UPDATE SET
      gross_attributed_gmv_cents = EXCLUDED.gross_attributed_gmv_cents,
      refund_amount_cents = EXCLUDED.refund_amount_cents,
      net_attributed_gmv_cents = EXCLUDED.net_attributed_gmv_cents,
      take_rate_bp = EXCLUDED.take_rate_bp,
      take_amount_cents = EXCLUDED.take_amount_cents,
      updated_at = NOW()

return len(results)
```

Batch the UPSERTs in a single transaction per call.

### `async def recompute_for_date(date: date, merchant_id: str) -> None`

Helper for dispute resolution. Re-aggregates a specific date + merchant only. Same logic as `aggregate_daily` but scoped to one merchant. Used by `apply_refund` after updating refund amounts.

### `async def apply_refund(edge_id: str, refund_amount_cents: int) -> None`

Called from the order refund webhook when a refund affects a GMV-attributed order.

```
BEGIN TRANSACTION
SELECT FOR UPDATE commerce_attribution_edges WHERE edge_id = :edge_id
UPDATE commerce_attribution_edges SET
    refund_amount_cents = COALESCE(refund_amount_cents, 0) + :refund_amount_cents,
    refunded_at = COALESCE(refunded_at, NOW())
WHERE edge_id = :edge_id
-- net_attributed_gmv_cents is a GENERATED ALWAYS AS STORED column; it auto-updates
COMMIT

await recompute_for_date(date=DATE(edge.created_at), merchant_id=edge.merchant_id)
```

Note: `net_attributed_gmv_cents` is a STORED generated column on `commerce_attribution_edges` (see migration 109). Do NOT attempt to write to it — Postgres auto-computes it as `GREATEST(gross_attributed_gmv_cents - refund_amount_cents, 0)`.

### Merchant promo cache

Module-level `_promo_cache: dict[str, tuple[datetime | None, float]]` mapping `merchant_id → (promo_period_until, fetched_at_epoch)`. TTL = 300 seconds. Query: `SELECT promo_period_until FROM merchants WHERE id = ... ` — BUT since billing tables use `merchant_id VARCHAR(50)` (operational string) and `merchants.id` is INTEGER, use `merchant_onboarding` or a direct lookup against the operational `merchant_id` string. Check `T2_db_audit.md` section 2 and 3 for the correct join path. If `merchant_onboarding` maps the string to the integer id, use it; otherwise query `merchants` with whatever column matches (check the merchants table for a `merchant_id` string column that may have been added).

## Acceptance criteria

- `services/gmv_aggregation_service.py` created with `aggregate_daily`, `recompute_for_date`, `apply_refund`.
- All UPSERT `ON CONFLICT` clauses use the expression form: `ON CONFLICT (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))`.
- Partial refunds reduce `net_attributed_gmv_cents` proportionally; net is floored at zero (a refund > gross does not produce negative GMV).
- `aggregate_daily` is idempotent — running it twice for the same date produces the same result (UPSERT, not INSERT).
- GMV = `subtotal - discount_total`; tax and shipping explicitly excluded.
- `net_attributed_gmv_cents` on `commerce_attribution_edges` is NOT written directly (it is a STORED generated column).
- Unit tests in `tests/test_gmv_aggregation_service.py` covering:
  - Simple aggregation (one edge, no refunds)
  - Partial refund (refund < gross)
  - Full refund (refund == gross → net = 0)
  - Refund larger than gross (refund > gross → net still = 0, not negative)
  - Promo → list take-rate transition (promo_period_until in the past → 1000 bp)
  - `aggregate_daily` idempotency (second call same date → same row count, same amounts)
  - `apply_refund` triggers `recompute_for_date` and updates `gmv_attribution_daily`

## Don't do

- Do NOT write the cron scheduler — that is wired separately.
- Do NOT include tax or shipping in GMV. v1.3 is explicit.
- Do NOT attempt to UPDATE `net_attributed_gmv_cents` on `commerce_attribution_edges` — it is a STORED generated column.
- Do NOT use `orders.total` as the GMV basis. Use `subtotal - discount_total`.
