# op-01 — Brand dispute out of window

**Authorization:** read-only investigation; resolution requires platform admin (manual ledger writes + Stripe credit note). Document every action in `credit_ledger.metadata` and `partner_balance_ledger.metadata` so the trail is auditable.

## Symptom

A merchant emails saying "I don't recognize this charge" or "this invoice is wrong" **AFTER** the standard 30-day reconciliation window has closed. The invoice is in `status='paid'`, `invoice_disputes` has no open row for it, and the in-app dispute path is no longer available.

Often surfaces as a chargeback intent ("if you don't fix this, I'm going to my bank") — handle quickly to avoid Stripe dispute fees.

## Investigation

1. Pull the invoice and confirm what's actually billed:
   ```sql
   SELECT i.id, i.merchant_id, i.stripe_invoice_id, i.total_cents,
          i.status, i.paid_at, i.billing_period_start, i.billing_period_end
   FROM invoices i WHERE i.merchant_id = '<merchant_id>'
     AND i.billing_period_start = '<period_start>';

   SELECT bri.source_type, bri.source_id, bri.amount_cents, bri.description, bri.voided_at
   FROM billing_run_items bri WHERE bri.merchant_id = '<merchant_id>'
     AND bri.stripe_invoice_id = '<stripe_invoice_id>';
   ```
2. Pull the underlying GMV rollups so you can show the merchant exactly what generated the charge:
   ```sql
   SELECT gad.date, gad.agent_id, gad.channel_partner_id, gad.gross_attributed_gmv_cents,
          gad.refund_amount_cents, gad.net_attributed_gmv_cents, gad.take_rate_bp, gad.take_amount_cents
   FROM gmv_attribution_daily gad
   WHERE gad.merchant_id = '<merchant_id>'
     AND gad.date BETWEEN '<period_start>' AND '<period_end>'
   ORDER BY gad.date;
   ```
3. Check whether a channel partner was paid against this invoice — that's what makes the reversal cross-cutting:
   ```sql
   SELECT ss.id AS snapshot_id, ss.channel_partner_id, ss.computed_comp_cents, ss.created_at,
          ap.id AS payout_id, ap.status, ap.amount, ap.confirmed_at
   FROM settlement_snapshots ss
   LEFT JOIN agent_payouts ap ON ap.snapshot_id = ss.id
   WHERE ss.billing_run_id = (SELECT billing_run_id FROM invoices WHERE id = <invoice_id>);
   ```

## Resolution

**Three writes, in this order. Each MUST link back to a written-up rationale in the metadata.**

1. **Stripe-side credit note** for the disputed amount (manually via Stripe Dashboard → Invoices → ... → Issue credit note):
   - Memo: "Out-of-window dispute resolution; see internal ref <ticket>"
   - Refund mode: depending on whether merchant already paid (refund) or didn't (just credit).
2. **Local `credit_ledger` entry** mirroring the credit note (use `operation_type='dispute_reversal'`):
   ```sql
   -- AUTHORIZATION REQUIRED
   INSERT INTO credit_ledger (merchant_id, operation_type, credits_delta, balance_after, source_type, metadata_json)
   VALUES ('<merchant_id>', 'dispute_reversal', 0, (SELECT balance FROM merchant_credits WHERE merchant_id = '<merchant_id>'),
           'out_of_window_dispute', '{"ticket":"<ticket>","stripe_credit_note_id":"<cn_...>","disputed_amount_cents":<n>}'::jsonb);
   ```
   (`credits_delta = 0` because this isn't a credit-balance change — it's a billing reversal. The row exists for audit.)
3. **Partner balance offset** if a channel partner was paid against the disputed invoice. Reduce their balance by the share they got attributed:
   ```sql
   INSERT INTO partner_balance_ledger (channel_partner_id, event_type, amount_cents, balance_after, metadata_jsonb)
   VALUES (<partner_id>, 'adjustment', -<partner_share_cents>,
           (SELECT balance_cents FROM partner_balance WHERE channel_partner_id = <partner_id>) - <partner_share_cents>,
           '{"ticket":"<ticket>","reason":"brand_dispute_out_of_window","disputed_invoice_id":<invoice_id>}'::jsonb);

   UPDATE partner_balance SET balance_cents = balance_cents - <partner_share_cents>
    WHERE channel_partner_id = <partner_id>;
   ```
   If the partner has already been paid out and the balance would go negative — that's fine, it carries to the next settlement. Per v1.3 §1.3 we don't claw back paid `agent_payouts` rows.

## Prevention

- **30-day soft window in admin UI** (not yet built): merchants can self-serve disputes in the first 30 days via the merchant portal → `invoice_disputes`. Out-of-window disputes still allowed but require admin approval (post-Markato).
- **90-day hard cutoff** (when admin UI lands): after 90 days from `invoices.paid_at`, this runbook becomes a "no, not this time" response — the partner cycle has already cleared and the variance is too disruptive.
- Track these incidents in `credit_ledger.metadata_json` so the post-Markato product team can size the v2 self-serve dispute window correctly.
