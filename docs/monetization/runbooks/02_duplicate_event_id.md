# 02 — Duplicate event_id collision

**Authorization:** read-only investigation; no resolution write required in steady state.

## Symptom

Stripe Dashboard shows multiple delivery attempts for the same `event_id` (e.g. `evt_3Q...`). All return 200 OK from Pivota. Logs show: `stripe_events.status='duplicate'` or no new row inserted on the second+ attempts.

This is **expected and correct behavior** — the runbook exists so the operator confirms it's not a real problem.

## Investigation

1. Query the `stripe_events` row for the event_id:
   ```sql
   SELECT id, event_id, event_type, status, processed_at, error, received_at
   FROM stripe_events
   WHERE event_id = 'evt_XXXXXXXXXX';
   ```
2. Confirm one row exists (the unique constraint `uq_stripe_events_event_id` prevents duplicates).
3. Confirm `status` is `processed`, `ignored`, or `failed` (not stuck `pending`).
4. If `status='failed'`: read the `error` column. The handler raised — go to runbook 03–05 depending on `event_type`.
5. If `status='pending'`: the handler crashed mid-process. Manually re-run by deleting and letting Stripe redeliver, OR replay via local handler (advanced — touch base with platform admin first).

## Resolution

- **Status = processed/ignored:** no action. T4's `INSERT ... ON CONFLICT (event_id) DO NOTHING` is the v1.3 idempotency contract working as designed.
- **Status = failed:** route to the appropriate failure-mode runbook based on `event_type`.
- **Status = pending (stuck):** this should never happen in steady state (the handler updates status before returning). If observed, capture the row, file a bug, and either wait for Stripe's next retry or manually invoke the handler.

## Prevention

- Already prevented by the unique constraint + `INSERT ... ON CONFLICT DO NOTHING` pattern in `routes/billing_routes.py`. No additional work for v1.3.
- v1.4: consider an `idx_stripe_events_status_received` partial index on `WHERE status IN ('pending', 'failed')` to make the "stuck handlers" query fast as `stripe_events` grows.
