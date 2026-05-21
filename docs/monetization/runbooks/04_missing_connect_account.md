# 04 — Missing connect_account on payout

**Authorization:** read-only investigation; resolution requires platform admin (UPDATE on `channel_partners`).

## Symptom

Payout approval fails with:
```
PayoutMissingConnectAccountError: Channel partner payout <payout_id> has no Connect account
```

`agent_payouts` row stays in `status='approved'` (or whatever pre-execute state) — no Stripe call was made, no balance was debited.

## Investigation

1. Identify the channel partner missing a Connect account:
   ```sql
   SELECT ap.id AS payout_id, ap.payee_id AS partner_id, cp.name,
          cp.stripe_connect_account_id, cp.commission_config_json
   FROM agent_payouts ap
   JOIN channel_partners cp ON cp.id = ap.payee_id
   WHERE ap.payee_type = 'channel_partner'
     AND ap.status IN ('approved', 'failed')
     AND (cp.stripe_connect_account_id IS NULL OR cp.stripe_connect_account_id = '');
   ```
2. Confirm with the partner whether they've completed Stripe Connect onboarding. Check Stripe Dashboard → Connected accounts for any account associated with their email/business.
3. If they HAVE completed Connect onboarding but the local row is missing the ID, fetch it:
   - Stripe Dashboard → Connected accounts → find by email/name → copy `acct_...` ID.

## Resolution

- **Partner has Stripe Connect account:**
  ```sql
  -- AUTHORIZATION REQUIRED. Verify partner identity match before running.
  UPDATE channel_partners
     SET stripe_connect_account_id = 'acct_XXXXXXXXXXXXXXXX'
   WHERE id = <partner_id> AND stripe_connect_account_id IS NULL;
  ```
  Then re-invoke `execute_payout(payout_id)` via the admin endpoint. The pending payout stays in `partner_balance` until execution succeeds.
- **Partner has NOT onboarded:** send them the Stripe Connect onboarding link. Mark the payout `status='waiting_onboarding'` (manually) so it doesn't get retried automatically.

## Prevention

- v1.3 service guards correctly — no broken Stripe call, no silent debit.
- Add a pre-flight check in the merchant/partner admin UI: "Channel partner cannot receive payouts until Connect onboarding is complete." Block the `create_payout` cron from rolling that partner up until `stripe_connect_account_id IS NOT NULL`.
- v1.4: nightly job that flags channel_partners with positive `partner_balance.balance_cents` AND missing `stripe_connect_account_id` — these are dormant funds and need a status owner.
