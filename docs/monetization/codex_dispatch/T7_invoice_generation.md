# Codex prompt — T7: Draft services/invoice_generation_service.py (corrected Stripe flow)

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Stack: Python (FastAPI), Postgres (Railway), Stripe (already integrated as PSP).
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` — implement v1.3 exactly, do not improvise on architecture.
Existing patterns to follow: see db/, services/, routes/, adapters/ — match style of existing files.
Output: code in the existing repo layout.
Don't add new dependencies unless absolutely required. Don't rewrite existing code unless explicitly asked.

## Prerequisite inputs — read these first

1. `docs/monetization/T1_stripe_codebase_audit.md` — Stripe integration conventions, especially the `stripe.StripeClient` pattern in `adapters/psp_adapter.py`.
2. `db/migrations/113_billing_core.sql` — `billing_runs`, `invoices`, `invoice_disputes`, `billing_run_items` schemas.
3. `db/migrations/110_gmv_attribution_daily.sql` — `gmv_attribution_daily` schema (source of truth for invoice line items).
4. `db/migrations/103_extend_merchants_monetization.sql` — `merchants.stripe_customer_id` (target customer for invoices).
5. `services/gmv_aggregation_service.py` (T6) — read this to understand how the GMV rollup data is structured.
6. `routes/billing_routes.py` (T4) — match the `stripe.StripeClient` instantiation pattern.

## Architecture decisions already made — implement exactly as specified

**Q1 — Stripe credentials:** Use `settings.stripe_secret_key` (the global platform key, `STRIPE_SECRET_KEY` env var) for all Invoice/InvoiceItem SDK calls. Do NOT use merchant-scoped PSP credentials. Instantiate `stripe.StripeClient(api_key=settings.stripe_secret_key)` at module level, same as T4.

## Critical Stripe flow lock-in (v1.3 §1.3) — DO NOT INVERT

The corrected Stripe invoice flow is:

1. **First**: Create a DRAFT Invoice with `stripe.Invoice.create(customer=..., collection_method='charge_automatically', auto_advance=False)`. Capture the `invoice.id`.
2. **Then**: For each line item, call `stripe.InvoiceItem.create(customer=..., invoice=<draft_invoice_id>, amount=..., currency='usd', description=...)` — the `invoice=<draft_id>` parameter is REQUIRED to attach the item to the specific draft invoice.

**DO NOT** create InvoiceItems without an `invoice=` parameter. Items without `invoice=` float to the customer's next automatically-generated subscription invoice — which is NOT what we want. They must land on the specific draft Invoice we just created.

**DO NOT** use `stripe.SubscriptionItem.create_usage_record` — that pattern is deprecated and not in v1.3.

If you find yourself wanting to call `InvoiceItem.create` before `Invoice.create`, stop and re-read this section. The order is Invoice-first, then InvoiceItems-with-invoice-id.

## Task

Create `services/invoice_generation_service.py` with the following functions:

### `async def run_billing_cycle(period_start: date, period_end: date) -> int`

Called by monthly cron. Returns `billing_run_id` (BIGINT from `billing_runs.id`).

Idempotency pattern:

```sql
INSERT INTO billing_runs (period_start, period_end, idempotency_key, status, created_at)
VALUES (:period_start, :period_end, :idempotency_key, 'running', NOW())
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id
```

Where `idempotency_key = f"{period_start.isoformat()}-billing"`.

If no row returned (already ran), SELECT the existing `id` by `idempotency_key` and return it. Do NOT re-run.

Then:
1. Query distinct `merchant_id` values from `gmv_attribution_daily` where `date BETWEEN period_start AND period_end` AND `take_amount_cents > 0`.
2. For each merchant: `await generate_merchant_invoice(billing_run_id, merchant_id, period_start, period_end)`. Catch + log per-merchant exceptions; do not abort the whole run on one failure.
3. UPDATE `billing_runs.status = 'completed', completed_at = NOW()` at the end.

Return `billing_run_id`.

### `async def generate_merchant_invoice(billing_run_id: int, merchant_id: str, period_start: date, period_end: date) -> str | None`

Returns `stripe_invoice_id`, or None if zero-GMV (no invoice generated).

1. **Look up merchant's Stripe customer:** `SELECT stripe_customer_id FROM merchants` (use the operational `merchant_id VARCHAR(50)` matching pattern — see T6's promo lookup query in `gmv_aggregation_service.py` for the join path through `user_subscriptions`). If `stripe_customer_id` is NULL, log warning and return None (merchant never went through Checkout signup).

2. **Read all `gmv_attribution_daily` rows** for this merchant + period where `take_amount_cents > 0`. If zero rows, return None (no invoice for this merchant this cycle).

3. **Create the DRAFT Invoice FIRST:**

   ```python
   invoice = await asyncio.to_thread(
       stripe_client.v1.invoices.create,
       params={
           "customer": stripe_customer_id,
           "collection_method": "charge_automatically",
           "auto_advance": False,  # v1 first cycle = manual finalize
           "description": f"Pivota – {period_start.strftime('%B %Y')}",
           "metadata": {
               "merchant_id": merchant_id,
               "billing_run_id": str(billing_run_id),
               "period_start": period_start.isoformat(),
               "period_end": period_end.isoformat(),
           },
       },
   )
   draft_invoice_id = invoice.id
   ```

4. **For each `gmv_attribution_daily` row, create an InvoiceItem with `invoice=draft_invoice_id`:**

   ```python
   item = await asyncio.to_thread(
       stripe_client.v1.invoice_items.create,
       params={
           "customer": stripe_customer_id,
           "invoice": draft_invoice_id,  # REQUIRED — attaches to the specific draft
           "amount": row["take_amount_cents"],
           "currency": "usd",
           "description": f"GMV Take Rate – Agent {row['agent_id'] or 'direct'} – {row['date']}",
           "metadata": {
               "merchant_id": merchant_id,
               "gmv_rollup_id": str(row["id"]),
               "billing_run_id": str(billing_run_id),
           },
       },
   )
   ```

5. **For each created InvoiceItem, INSERT into `billing_run_items`:**

   ```sql
   INSERT INTO billing_run_items (
     billing_run_id, merchant_id, source_type, source_id,
     stripe_invoice_item_id, stripe_invoice_id, amount_cents, description
   ) VALUES (
     :billing_run_id, :merchant_id, 'gmv_rollup', :gmv_attribution_daily_id,
     :stripe_invoice_item_id, :draft_invoice_id, :amount_cents, :description
   )
   ```

6. **INSERT into `invoices`:**

   ```sql
   INSERT INTO invoices (
     merchant_id, billing_period_start, billing_period_end, billing_run_id,
     stripe_invoice_id, stripe_customer_id, total_cents, status, due_date, created_at
   ) VALUES (
     :merchant_id, :period_start, :period_end, :billing_run_id,
     :draft_invoice_id, :stripe_customer_id, :total_cents, 'draft', :period_end_plus_5, NOW()
   )
   ```

   where `total_cents = SUM(take_amount_cents)` across the items, and `due_date = period_end + INTERVAL '5 days'`.

7. Return `draft_invoice_id`.

Wrap steps 3-6 in a try/except: if Stripe SDK calls fail mid-way, log the partial state and re-raise — do NOT leave orphaned `billing_run_items` without a matching `invoices` row.

### `async def finalize_invoice(stripe_invoice_id: str) -> None`

Called by admin in v1 (manual), by cron in v1.1+.

1. UPDATE `invoices.status = 'finalizing'` for the matching `stripe_invoice_id`.
2. `await asyncio.to_thread(stripe_client.v1.invoices.finalize_invoice, stripe_invoice_id, params={"auto_advance": True})`.
3. The local `invoices` row will transition to `'finalized'` when the `invoice.finalized` webhook fires (handled by T4). This function does not update the local status directly past `'finalizing'`.

### `async def handle_dispute(invoice_dispute_id: int) -> None`

Called when a merchant files a dispute via the merchant portal (out-of-scope route, but this service function is the entry point).

1. SELECT the `invoice_disputes` row by `id = invoice_dispute_id`. Read `invoice_id` (FK to `invoices`), `disputed_line_items_jsonb` (JSON array of `billing_run_items.id`s and their adjusted amounts).

2. SELECT the `invoices` row. Verify `status = 'draft'` (Stripe only allows InvoiceItem deletion on draft invoices). If finalized, raise `DisputeOnFinalizedInvoiceError`.

3. For each disputed line item:
   - SELECT the `billing_run_items` row to find `stripe_invoice_item_id`.
   - `await asyncio.to_thread(stripe_client.v1.invoice_items.delete, stripe_invoice_item_id)` — voids it on the draft.
   - If adjusted_amount > 0: create a replacement InvoiceItem with the new amount using the SAME `invoice=draft_invoice_id` pattern as `generate_merchant_invoice`. INSERT new `billing_run_items` row.
   - Mark the original `billing_run_items` row with `voided_at = NOW()` (add column via the same defensive ALTER pattern T4/T5 used for missing columns if needed — or just store in metadata if the column isn't there).

4. UPDATE `invoice_disputes.status = 'applied', resolved_at = NOW()`.

## Style requirements

- `stripe.StripeClient` instantiated at module level using `settings.stripe_secret_key`.
- All Stripe SDK calls wrapped with `asyncio.to_thread(...)`.
- Type hints + docstrings on every public function.
- Define a custom exception class hierarchy at the top: `InvoiceGenerationError`, `DisputeOnFinalizedInvoiceError`, `MerchantNotEnrolledError` (no `stripe_customer_id`).
- Match the import + database pattern from `services/gmv_aggregation_service.py` (T6).

## Acceptance criteria

- `services/invoice_generation_service.py` created with all 4 public functions.
- **Invoice flow ordering is EXPLICITLY Invoice-first, InvoiceItems-with-invoice-id-second.** Every `InvoiceItem.create` call passes `invoice=<draft_id>`. No InvoiceItem is ever created without that parameter.
- `auto_advance=False` on the initial `Invoice.create` call (v1 manual finalize).
- `billing_run_items` row created for every Stripe InvoiceItem, with `source_type='gmv_rollup'` and `source_id` matching the `gmv_attribution_daily.id`.
- `run_billing_cycle` is idempotent on `billing_runs.idempotency_key`; duplicate cron runs return the existing `billing_run_id` and do not re-execute.
- Zero-GMV merchant produces no invoice (return None).
- Dispute mid-cycle replaces the original line item via `invoice_items.delete` + new `invoice_items.create` — not via amount modification.
- Unit tests in `tests/test_invoice_generation_service.py` covering:
  - Happy path: one merchant, multiple GMV rollups → one invoice with N items
  - Idempotent re-run of `run_billing_cycle` → same `billing_run_id`, no duplicate API calls
  - Dispute mid-cycle (line item replaced)
  - Zero-GMV merchant (no invoice generated)
  - Merchant without `stripe_customer_id` (logs warning, skips)
  - Stripe SDK failure mid-loop (partial state logged, exception re-raised)

## Don't do

- Do NOT use `stripe.SubscriptionItem.create_usage_record`. It is deprecated and not in v1.3.
- Do NOT create InvoiceItems without an `invoice=` parameter. They will float to the next subscription invoice.
- Do NOT invert the order: it is `Invoice.create` first, then `InvoiceItem.create(invoice=...)`. Not the reverse.
- Do NOT set `auto_advance=True` on the initial `Invoice.create` — v1 is manual finalize.
- Do NOT use merchant PSP credentials. Use platform `settings.stripe_secret_key`.
- Do NOT modify the `invoice.finalized` webhook handler in `routes/billing_routes.py` — that's T4's territory.
