# op-02 — Markato payout short

**Authorization:** read-only investigation; resolution requires platform admin (catch-up settlement run + ledger adjustment if real error found).

## Symptom

Markato (or any channel partner) emails saying "my payout was lower than expected" or "I got paid $X but I think the brands I drove generated $Y in GMV-take." Variance is real money — handle within 24h.

## Investigation

Produce a one-page statement showing the math. v1.3 §1.3 partner comp formula:

```
net_comp = subscription_rev_share + credit_overage_share + gmv_take_share + cohort_bonus
           − subsidy_cap_applied
           − clawback_applied
```

1. Period boundary: confirm which billing period the partner is asking about. Map to `billing_runs.idempotency_key = '<YYYY-MM-DD>-billing'`.

2. Read the immutable snapshot for that period:
   ```sql
   SELECT ss.id, ss.computed_comp_cents, ss.subsidy_cap_remaining_at_snapshot,
          ss.snapshot_payload_jsonb, ss.created_at
   FROM settlement_snapshots ss
   WHERE ss.channel_partner_id = <partner_id>
     AND ss.billing_run_id = (SELECT id FROM billing_runs WHERE idempotency_key = '<YYYY-MM-DD>-billing');
   ```
   `snapshot_payload_jsonb` contains the inputs (per-brand contribution, subsidy cap state, clawback list). Use it as the basis for the merchant-facing statement.

3. Cross-check the snapshot against the underlying source data:
   - **Subscription revenue**:
     ```sql
     SELECT i.merchant_id, i.total_cents AS invoice_total, i.status
     FROM invoices i JOIN partner_attribution pa ON pa.merchant_id = i.merchant_id
     WHERE pa.channel_partner_id = <partner_id>
       AND i.billing_period_start = '<period_start>';
     ```
     Only invoices with `status = 'paid'` count (nonpayment rule, v1.3 §1.3).
   - **GMV-take share**:
     ```sql
     SELECT gad.merchant_id, SUM(gad.take_amount_cents) AS take_total
     FROM gmv_attribution_daily gad
     JOIN invoices i ON i.merchant_id = gad.merchant_id
          AND gad.date BETWEEN i.billing_period_start AND i.billing_period_end
     WHERE gad.channel_partner_id = <partner_id>
       AND gad.date BETWEEN '<period_start>' AND '<period_end>'
       AND i.status = 'paid'
     GROUP BY gad.merchant_id;
     ```
     Apply `commission_config_json.gmv_take_share_bp` from `channel_partners` to get the partner's share.
   - **Subsidy cap & clawback**: from `snapshot_payload_jsonb.clawbacks[]` and `snapshot_payload_jsonb.subsidy_cap_applied_cents`.

4. Tally against `partner_balance_ledger`:
   ```sql
   SELECT event_type, amount_cents, balance_after, created_at, metadata_jsonb
   FROM partner_balance_ledger
   WHERE channel_partner_id = <partner_id>
   ORDER BY created_at DESC LIMIT 30;
   ```
   Should show: `settlement_added` (+credit), `clawback` (-debit) per impacted brand, `payout` (-debit) when paid out.

5. Compare the partner's calculation to yours. If they match: the partner misunderstood the formula or had stale expectations — send them the one-page statement. If they don't match: find the discrepancy and proceed to resolution.

## Resolution

- **Math agrees, partner is mistaken:** send the statement (per-line breakdown from steps 2–4). Optionally walk through on a call. No DB writes.
- **Real error in our favor (we underpaid):** issue a catch-up payment via `partner_balance_ledger` adjustment:
  ```sql
  -- AUTHORIZATION REQUIRED
  INSERT INTO partner_balance_ledger (channel_partner_id, event_type, amount_cents, balance_after, metadata_jsonb)
  VALUES (<partner_id>, 'adjustment', +<missing_cents>,
          (SELECT balance_cents FROM partner_balance WHERE channel_partner_id = <partner_id>) + <missing_cents>,
          '{"ticket":"<ticket>","reason":"payout_short_resolution","prior_snapshot_id":<id>,"correction_source":"<calc_source>"}'::jsonb);
  UPDATE partner_balance SET balance_cents = balance_cents + <missing_cents> WHERE channel_partner_id = <partner_id>;
  ```
  Next settlement cron will pick up the increased balance and pay it out.
- **Real error in our error's favor (we overpaid):** harder — the prior payout is already settled. Document the discrepancy in `partner_balance_ledger` with `event_type='adjustment'`, `amount_cents=-<overpaid>`. Future earnings absorb the negative balance. Communicate clearly with the partner.

Never modify `settlement_snapshots` rows — they're append-only by trigger. New evidence = new snapshot (via `write_settlement_snapshot`), not edit of old.

## Prevention

- **v1.4: weekly variance check cron** (post-Markato): for every active channel_partner, compute `expected_comp` from raw sources (`invoices` + `gmv_attribution_daily`) and compare to the most recent `settlement_snapshots.computed_comp_cents`. Alert on |delta| > $50 OR > 5%.
- **Pre-payout sanity check**: before `create_payout` runs in the settlement cron, recompute partner comp from scratch and assert it matches the snapshot to the cent. If they don't match (shouldn't ever, given snapshot is immutable), abort the payout and page.
- **Partner-facing statement automation**: build a `/partner/statements/{billing_run_id}` endpoint that serves the one-page math automatically. Cuts response time from hours to seconds, sets expectations clearly each cycle.
