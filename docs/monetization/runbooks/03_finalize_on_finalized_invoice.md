# 03 — Finalize on already-finalized invoice

**Authorization:** read-only investigation; resolution requires platform admin (UPDATE on `invoices`).

## Symptom

`invoices.status='finalizing'` and is not progressing to `finalized`. Admin observes the invoice in Stripe Dashboard already shows `status: finalized` (or `paid`). Application logs show:

```
RuntimeError: Invoice is already finalized
```

or a `stripe.error.InvalidRequestError` from `finalize_invoice`.

## Investigation

1. Find affected invoices:
   ```sql
   SELECT id, merchant_id, stripe_invoice_id, status, billing_period_start, created_at
   FROM invoices
   WHERE status = 'finalizing'
     AND created_at < NOW() - INTERVAL '15 minutes'
   ORDER BY created_at ASC;
   ```
2. For each `stripe_invoice_id`, check the current state in Stripe (Dashboard or `stripe.Invoice.retrieve`). If Stripe reports `finalized`, `paid`, `uncollectible`, or `void`, the local mirror is just behind.
3. Confirm whether the `invoice.finalized` webhook fired but failed to update the local row:
   ```sql
   SELECT id, event_type, status, error, received_at
   FROM stripe_events
   WHERE payload_jsonb->'data'->'object'->>'id' = '<stripe_invoice_id>'
   ORDER BY received_at DESC;
   ```

## Resolution

- **Stripe says finalized, local says finalizing:** the `invoice.finalized` webhook may have been dropped or arrived before the local row existed. Sync manually:
  ```sql
  -- AUTHORIZATION REQUIRED before running
  UPDATE invoices SET status = 'finalized'
   WHERE id = <invoice_id> AND status = 'finalizing';
  ```
- **Stripe says paid:** sync to `paid`:
  ```sql
  UPDATE invoices SET status = 'paid', paid_at = NOW()
   WHERE id = <invoice_id> AND status IN ('finalizing', 'finalized');
  ```
- **Stripe says draft (rare; finalize never reached Stripe):** re-invoke `finalize_invoice(stripe_invoice_id)` via the admin endpoint after fixing the underlying error.

## Prevention

- The harness already exercises this mode (`test_failure_modes::finalize on finalized`). v1.3 service is correct — the failure is webhook delivery, not code.
- v1.4: add a reconciliation cron that scans `invoices.status='finalizing'` older than 5 minutes and pulls fresh state from Stripe, auto-syncing.
