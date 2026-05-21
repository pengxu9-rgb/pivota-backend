# Codex prompt — T9: Stamp `gross_attributed_gmv_cents` in commerce payment flow

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx`

This task was identified during Wave 3 review. **Design gap:** v1.3 introduces the column `commerce_attribution_edges.gross_attributed_gmv_cents` and T6's GMV aggregation reads from it, but nothing in the current commerce-payment flow populates it. Without T9, T7's invoice generation runs against empty data and the v1.3 pipeline can't be tested end-to-end.

T9 closes that gap. The stamping hook lives commerce-side (in the existing PSP / payment-finalization flow), NOT in T4's new billing_routes.py.

## Parallelism

Run in parallel with T7 and T8 in Wave 4. No dependencies beyond T3 (already done). Strictly additive — extends an existing service with one new step.

## Files to read

```
services/psp_payment_finalizer.py
db/orders.py
db/commerce_attribution.py
routes/webhook_routes.py   (to confirm where payment_intent.succeeded hands off)
docs/monetization/T1_stripe_codebase_audit.md  (for the upstream flow)
docs/monetization/T2_db_audit.md               (for column types and conventions)
```

## Task

Add a step inside `services/psp_payment_finalizer.py` (or a tightly-scoped helper it calls) that runs after `finalize_payment_success` (or whatever the equivalent post-commerce-payment hook is named in the existing code — confirm from T1 audit) and stamps the GMV column on attribution edges associated with the just-paid order.

### Stamping logic

For each `commerce_attribution_edges` row where `order_id = <just-paid order>` AND `gross_attributed_gmv_cents IS NULL`:

```sql
UPDATE commerce_attribution_edges
SET gross_attributed_gmv_cents = :gross
WHERE order_id = :order_id
  AND gross_attributed_gmv_cents IS NULL
```

Where `:gross` is computed as: `order.subtotal_cents - order.discount_total_cents`. Exact column names per T2 audit. Tax and shipping are **excluded** — v1.3 §1.3 GMV definition is strict.

### Constraints

- **Idempotent:** The `IS NULL` predicate guarantees a second run is a no-op. Webhook retries must not double-stamp.
- **Non-blocking:** If no `commerce_attribution_edges` rows exist for the order, the function logs and returns silently. A commerce order without attribution (direct DTC, no agent involvement) is valid; it just produces zero billable GMV.
- **No FK constraint additions in this task** — the relationship between orders and commerce_attribution_edges is already established in the existing schema; T9 just populates a column.
- **Don't touch `net_attributed_gmv_cents`** — that column is `GENERATED ALWAYS AS ... STORED` (migration 109) and updates automatically when refund_amount_cents changes. T9 only populates gross.
- **Don't touch refund-side logic** — T6's `apply_refund` function owns refund accounting. T9 is strictly the initial stamping at first-payment time.

## Output

1. **Modify** `services/psp_payment_finalizer.py` to add the stamping step. Match the existing module's style (async/sync, error handling, dependency injection).
2. **Add** a new helper function (probably `stamp_gross_attributed_gmv(order_id)` or similar) that does the actual SQL. Keep it small and unit-testable.
3. **Write** `tests/test_attribution_stamping.py` with these cases:
   - Standard order with one attribution edge — gmv stamped correctly.
   - Order with multiple attribution edges (one for each surface_click_event) — all edges stamped.
   - Order with no attribution edges — silently no-ops.
   - Order with `gross_attributed_gmv_cents` already set on the edge (retry case) — IS NULL clause skips it.
   - Order with subtotal=0 — edge stamped with 0 (still a valid stamping, treated as "ad-hoc-rated GMV is zero").
   - Order with discount > subtotal — gross is clamped to zero (defensive; shouldn't happen in real data but guard against it).

## Acceptance criteria

- Hook lives in `services/psp_payment_finalizer.py` or a sub-helper it calls. Not in `routes/`.
- Call site is wherever `finalize_payment_success` (or equivalent) lands today after a commerce PaymentIntent clears.
- SQL is exactly the `IS NULL`-guarded UPDATE above; no clever batching or row-locking — one targeted UPDATE per order.
- Tax + shipping explicitly excluded from the gross computation. The docstring + an inline comment must state this.
- Tests pass.
- No modifications to T4's `routes/billing_routes.py`, T5's `services/metering_service.py`, T6's `services/gmv_aggregation_service.py`, or any migrations.

## Don't do

- Don't add a new commerce webhook endpoint. T9 hooks into the existing `payment_intent.succeeded` flow that already routes through `services/psp_payment_finalizer.py`.
- Don't compute `net_attributed_gmv_cents` — that's a generated column.
- Don't introduce a new dependency or library.
- Don't add backfill logic for historical orders — that's a separate one-shot script if/when needed; not in T9 scope.
- Don't touch refund handling. T6 owns it.
- Don't try to handle "what if the order is partially-fulfilled or partially-paid" — assume order.status = paid is the trigger and the order amount is the gross. v1 keeps it simple.
