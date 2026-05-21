# 05 — Stripe Transfer failure mid-settlement

**Authorization:** read-only investigation; resolution may require platform-balance top-up.

## Symptom

`agent_payouts.status='failed'`. Partner's `partner_balance.balance_cents` is still positive (NOT debited — `execute_payout`'s catch path is correct). `agent_payouts.error_message` (or `metadata.stripe_transfer_error`) shows a Stripe error like `insufficient_funds`, `account_invalid`, `transfer_authorization_required`, etc.

## Investigation

1. Identify the failed payout(s):
   ```sql
   SELECT ap.id, ap.payee_id, ap.amount, ap.billing_run_id,
          COALESCE(ap.error_message, ap.metadata::text) AS error,
          ap.updated_at
   FROM agent_payouts ap
   WHERE ap.status = 'failed'
     AND ap.payee_type = 'channel_partner'
   ORDER BY ap.updated_at DESC
   LIMIT 20;
   ```
2. Verify the balance was NOT debited (it shouldn't be — but confirm):
   ```sql
   SELECT balance_cents FROM partner_balance WHERE channel_partner_id = <payee_id>;
   SELECT event_type, amount_cents, created_at FROM partner_balance_ledger
    WHERE channel_partner_id = <payee_id>
    ORDER BY created_at DESC LIMIT 5;
   ```
   Confirm the most recent entries are `settlement_added` events, NOT `payout` events.
3. Categorize the error:
   - `insufficient_funds` → platform Stripe balance is too low.
   - `account_invalid` / `account_closed` → partner's Connect account is bad.
   - `amount_too_small` / `amount_too_large` → Stripe rejection threshold (rare for monthly settlement amounts).
   - other → likely transient; can be retried as-is.

## Resolution

- **Insufficient platform balance:** top up the platform Stripe account, then re-invoke `execute_payout(payout_id)` via the admin endpoint. The payout row's status becomes `paid` and `partner_balance` is debited then.
- **Account invalid:** see runbook 04. Update `channel_partners.stripe_connect_account_id` (if partner re-onboarded under a new account) or escalate to partner ops.
- **Transient error:** simply re-invoke `execute_payout(payout_id)`. Idempotency lives at the `agent_payouts` row level — re-execution starts from the `approved` state.

DO NOT manually debit `partner_balance` to "match" the failed payout. The whole point of v1.3 §1.3 is that balance reflects what's actually owed.

## Prevention

- Pre-flight balance check in the settlement cron: if estimated total payouts > platform Stripe balance, hold and alert before any `agent_payouts` row gets created.
- v1.4: weekly variance check (see runbook op-02) catches drift between expected and actual payout amounts.
