# 06 — Partial billing run failure (v1.4 gap workaround)

**Authorization:** read-only investigation; resolution requires platform admin (manual INSERT/UPDATE).

## Symptom

`run_billing_cycle` completes (no exception, `billing_runs.status='completed'`) but one or more merchants are missing from `invoices` for that period. Operator notices via reconciliation: GMV-take revenue is lower than expected, or a brand reports they weren't invoiced.

## Investigation

1. Confirm `billing_runs` row exists for the period:
   ```sql
   SELECT id, period_start, period_end, idempotency_key, status, completed_at
   FROM billing_runs
   WHERE idempotency_key = '<YYYY-MM-DD>-billing';
   ```
2. Find merchants who had GMV in the period but no invoice:
   ```sql
   SELECT gad.merchant_id, SUM(gad.take_amount_cents) AS expected_take_cents
   FROM gmv_attribution_daily gad
   LEFT JOIN invoices i ON i.merchant_id = gad.merchant_id
        AND i.billing_period_start = <period_start>
   WHERE gad.date BETWEEN <period_start> AND <period_end>
     AND gad.take_amount_cents > 0
     AND i.id IS NULL
   GROUP BY gad.merchant_id;
   ```
3. Check application logs for `services.invoice_generation_service` exceptions during the cron run. T7 catches per-merchant exceptions and logs `Merchant invoice generation failed billing_run_id=X merchant_id=Y`. Grep that.

## Resolution

Manual catch-up for each affected merchant. Re-running `run_billing_cycle` is a no-op (idempotency on `idempotency_key`) — v1.3 has no automatic per-merchant retry.

For each missing merchant:

```sql
-- AUTHORIZATION REQUIRED. Verify merchant + amount manually before running.
-- 1. Confirm what should have been billed
SELECT date, agent_id, channel_partner_id, take_amount_cents
FROM gmv_attribution_daily
WHERE merchant_id = '<merchant_id>'
  AND date BETWEEN <period_start> AND <period_end>
ORDER BY date;
```

Then manually invoke `generate_merchant_invoice(billing_run_id, merchant_id, period_start, period_end)` via the admin REPL or a one-off admin endpoint. Pass the existing `billing_run_id` from step 1 — the new InvoiceItems will associate to it.

After Stripe Invoice + InvoiceItems land, verify:

```sql
SELECT id, stripe_invoice_id, total_cents, status FROM invoices
WHERE merchant_id = '<merchant_id>' AND billing_period_start = <period_start>;

SELECT source_type, source_id, stripe_invoice_item_id, amount_cents
FROM billing_run_items
WHERE billing_run_id = <billing_run_id> AND merchant_id = '<merchant_id>';
```

## Prevention

- **v1.4 (post-Markato):** add `retry_failed_billing_run_items` service method + admin endpoint. Codify the manual workaround above as: failed merchants get a `billing_run_items` row with `source_type='generation_failed'`; an admin button retries them without breaking T7's period-level idempotency. See `questions_for_cowork.md` "v1.4 design followup".
- **v1.3 monitoring:** post-billing-cron reconciliation query that compares `SUM(gmv_attribution_daily.take_amount_cents)` to `SUM(invoices.total_cents)` for the period; alert if delta > 5%.
- **v1.4 schema:** consider adding `billing_runs.status='partial_failure'` (vs `completed`) and counting failed merchants in `billing_runs.metadata_jsonb` so the reconciliation gap is loud, not silent.
