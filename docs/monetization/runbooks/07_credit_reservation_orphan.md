# 07 — Orphaned credit reservation past expiry

**Authorization:** read-only investigation; resolution by the reaper cron (no human action in steady state).

## Symptom

Merchant complains their `credits_balance` is lower than expected, OR a `credit_reservations` row sits in `status='reserved'` past its `expires_at`. The reaper cron (`expire_stale_reservations`) should auto-release these every 5 minutes — if it's been stuck longer, something is wrong with the cron.

## Investigation

1. Find stale reservations:
   ```sql
   SELECT id, merchant_id, operation_type, operation_id,
          credits_held, expires_at, status, finalized_at
   FROM credit_reservations
   WHERE status = 'reserved'
     AND expires_at < NOW() - INTERVAL '10 minutes'
   ORDER BY expires_at ASC;
   ```
   Expectation in steady state: zero rows. Anything > 0 means the reaper isn't running.
2. Verify the reaper is configured. Check the scheduler logs for `expire_stale_reservations` ticks. Also check application logs for "[metering] expire_stale_reservations released N reservations" or equivalent.
3. Cross-check the merchant's balance vs. the ledger:
   ```sql
   SELECT mc.balance, COALESCE(SUM(cl.credits_delta), 0) AS ledger_sum
   FROM merchant_credits mc
   LEFT JOIN credit_ledger cl ON cl.merchant_id = mc.merchant_id
   WHERE mc.merchant_id = '<merchant_id>'
   GROUP BY mc.balance;
   ```
   `balance` should equal `ledger_sum`. If they diverge, a write was skipped — investigate, do not blindly fix.

## Resolution

- **Reaper not running:** restart the scheduler service. Each reaper tick processes ALL stale reservations in one batch — once it catches up, the queue clears.
- **Reaper running but reservation not expiring:** check the reservation's `status` column under read-committed isolation. T5 uses `SELECT FOR UPDATE` — a long-running uncommitted transaction elsewhere can block the reaper. Find the blocker:
  ```sql
  SELECT pid, state, query_start, wait_event_type, wait_event, query
  FROM pg_stat_activity
  WHERE state != 'idle' AND query_start < NOW() - INTERVAL '1 minute';
  ```
  Kill the offending session if it's a known-stuck process.
- **Manual release (last resort):** if the reaper is broken and credits MUST be released now:
  ```sql
  -- AUTHORIZATION REQUIRED. Reaper should handle this; only run if reaper is offline.
  -- Call services.metering_service.release(reservation_id) via admin REPL, OR:
  BEGIN;
  UPDATE credit_reservations SET status = 'expired', finalized_at = NOW() WHERE id = <id> AND status = 'reserved';
  UPDATE merchant_credits SET balance = balance + <credits_held> WHERE merchant_id = '<merchant_id>';
  INSERT INTO credit_ledger (merchant_id, operation_type, credits_delta, balance_after, reservation_id, source_type)
   VALUES ('<merchant_id>', 'reservation_expired', <credits_held>, (SELECT balance FROM merchant_credits WHERE merchant_id = '<merchant_id>'), <id>, 'expiry');
  COMMIT;
  ```
  Prefer calling the service method over raw SQL — fewer ways to get the ledger wrong.

## Prevention

- Monitor `credit_reservations` count where `status='reserved' AND expires_at < NOW() - INTERVAL '10 minutes'` on a 1-minute interval. Alert if > 0 for more than 2 consecutive checks.
- Monitor reaper tick latency; if `expire_stale_reservations` takes > 30s on any single run, investigate growing reservation table size.
