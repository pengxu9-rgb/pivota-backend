# Codex prompt — T8: Draft services/partner_settlement_service.py + Test Clock harness

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Stack: Python (FastAPI), Postgres (Railway), Stripe (already integrated as PSP).
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` — implement v1.3 exactly, do not improvise on architecture.
Existing patterns to follow: see db/, services/, routes/, adapters/ — match style of existing files.
Output: code in the existing repo layout.
Don't add new dependencies unless absolutely required. Don't rewrite existing code unless explicitly asked.

## Prerequisite inputs — read these first

1. `docs/monetization/T1_stripe_codebase_audit.md` — Stripe integration conventions, `stripe.StripeClient` pattern.
2. `db/migrations/108_channel_partners.sql` — channel partner registry + `commission_config_json`, `connect_account_id`.
3. `db/migrations/111_partner_attribution.sql` — maps attribution edges to channel partners.
4. `db/migrations/112_partner_balance.sql` — running balance per partner.
5. `db/migrations/113_billing_core.sql` — `billing_runs`, `invoices` (need to check invoice paid status for nonpayment rule).
6. `db/migrations/114_settlement_snapshots.sql` — immutable snapshot table; append-only trigger.
7. `db/migrations/115_partner_balance_ledger.sql` — append-only ledger; clawback events live here.
8. `db/migrations/116_extend_agent_payouts_monetization.sql` — extended agent_payouts (status/payee_type/subsidy_cap_remaining_cents/clawback_amount_cents).
9. `services/invoice_generation_service.py` (T7) — match `stripe.StripeClient` instantiation pattern.

## Architecture decisions already made — implement exactly as specified

**Q1 — Stripe credentials:** Use `settings.stripe_secret_key` (platform key) for all `stripe.Transfer.create` calls. Do NOT use merchant PSP credentials. The platform pays partners from its own Stripe balance.

## Critical clawback lock-in (v1.3 §1.3) — DO NOT INVERT

The clawback mechanism is a **ledger debit** on `partner_balance_ledger`, NOT a Stripe Transfer reversal.

When a brand churns within 90 days and you need to claw back accrued comp on that brand:

```sql
INSERT INTO partner_balance_ledger (channel_partner_id, event_type, amount_cents, source_billing_run_id, source_snapshot_id, created_at)
VALUES (:partner_id, 'clawback', -:amount_cents, :billing_run_id, :snapshot_id, NOW())
-- Then:
UPDATE partner_balance SET balance_cents = balance_cents - :amount_cents WHERE channel_partner_id = :partner_id
```

This reduces the partner's running balance. The next `create_payout` call will see the reduced balance and either pay out less, or carry a zero/negative balance to the next period.

**DO NOT** call `stripe.Transfer.create_reversal` for clawbacks. That mechanism depends on a Stripe Connect transfer existing AND the partner's Connect-account balance still holding the funds, which we cannot guarantee. The ledger debit is the source of truth.

**DO NOT** reverse a paid `agent_payouts` row to claw back. Once paid, it's paid. Clawback applies to FUTURE balance.

## Task

### Part 1 — `services/partner_settlement_service.py`

Create with all 7 functions below.

#### `async def run_settlement(billing_run_id: int) -> int`

Called after `run_billing_cycle` (T7) completes. Returns count of payout rows created.

```
SELECT channel_partner_id FROM partner_attribution pa
  JOIN commerce_attribution_edges cae ON cae.edge_id = pa.edge_id
  WHERE DATE(cae.created_at) BETWEEN period_start AND period_end
  GROUP BY channel_partner_id

FOR EACH channel_partner_id:
    comp_dict = await compute_partner_comp(channel_partner_id, period_start, period_end)
    snapshot_id = await write_settlement_snapshot(billing_run_id, channel_partner_id, comp_dict)
    if comp_dict["net_comp_cents"] > 0:
        await credit_partner_balance(channel_partner_id, comp_dict["net_comp_cents"], snapshot_id)
    for clawback in comp_dict["clawbacks"]:
        await debit_partner_balance(channel_partner_id, clawback["amount_cents"], event_type='clawback', metadata=clawback)
    payout_row = await create_payout(channel_partner_id, billing_run_id, snapshot_id)
    if payout_row: count += 1

return count
```

Read `period_start`/`period_end` from `billing_runs` by `id = billing_run_id`.

#### `async def compute_partner_comp(channel_partner_id: int, period_start: date, period_end: date) -> dict`

Returns dict with keys: `subscription_rev_cents`, `gmv_take_rev_cents`, `credit_overage_rev_cents`, `subsidy_cap_applied_cents`, `clawbacks` (list), `net_comp_cents`.

1. Read `channel_partners.commission_config_json` — contains `subscription_rev_share_bp`, `gmv_take_share_bp`, `subsidy_cap_cents`, brand-attribution rules.
2. Sum subscription revenue:
   ```sql
   SELECT SUM(i.total_cents) FROM invoices i
   JOIN partner_attribution pa ON pa.merchant_id = i.merchant_id
   WHERE pa.channel_partner_id = :partner_id
     AND i.billing_period_start = :period_start
     AND i.status = 'paid'  -- nonpayment rule: only paid invoices count
   ```
   Apply `subscription_rev_share_bp` from config.
3. Sum GMV-take revenue:
   ```sql
   SELECT SUM(gad.take_amount_cents) FROM gmv_attribution_daily gad
   JOIN invoices i ON i.merchant_id = gad.merchant_id AND i.billing_period_start <= gad.date AND gad.date <= i.billing_period_end
   WHERE gad.channel_partner_id = :partner_id
     AND gad.date BETWEEN :period_start AND :period_end
     AND i.status = 'paid'  -- nonpayment rule
   ```
   Apply `gmv_take_share_bp`.
4. Credit overage revenue = 0 in v1 (prepaid model, no overage).
5. Apply subsidy cap:
   - For each attributed brand, look up the current `subsidy_cap_remaining_cents` from the most recent `agent_payouts` row for that brand (FK: `payee_type='channel_partner'`, `payee_id=channel_partner_id`).
   - Cap this period's accrual on that brand at the remaining subsidy_cap. Track decrement.
6. Apply churn clawback:
   - For each attributed brand where `merchants.subscription_id IS NULL` AND brand was active within last 90 days (`merchants.subscription_canceled_at > NOW() - INTERVAL '90 days'` if such a column exists; otherwise check `user_subscriptions.status='canceled' AND canceled_at > NOW() - INTERVAL '90 days'`):
     - Look up accrued comp on that brand from `settlement_snapshots` history (sum over prior snapshots).
     - Append to `clawbacks` list: `{"merchant_id": ..., "amount_cents": ..., "reason": "90_day_churn"}`.

Return the dict. `net_comp_cents = subscription_rev_cents + gmv_take_rev_cents + credit_overage_rev_cents - subsidy_cap_applied_cents - sum(clawback amounts)`. Floor at zero — do NOT return negative `net_comp_cents`; clawback amounts that exceed accrual carry to balance as a debit instead.

#### `async def write_settlement_snapshot(billing_run_id: int, channel_partner_id: int, snapshot_payload: dict) -> int`

```sql
INSERT INTO settlement_snapshots (billing_run_id, channel_partner_id, snapshot_payload_jsonb, computed_comp_cents, subsidy_cap_remaining_at_snapshot, created_at)
VALUES (:billing_run_id, :channel_partner_id, :snapshot_payload, :computed_comp, :subsidy_cap_remaining, NOW())
RETURNING id
```

Returns the new snapshot id. Append-only (the table has a trigger blocking UPDATE/DELETE — see migration 114).

#### `async def credit_partner_balance(channel_partner_id: int, amount_cents: int, snapshot_id: int) -> None`

```sql
BEGIN TRANSACTION
-- Use INSERT ... ON CONFLICT DO NOTHING to UPSERT the partner_balance row if first ever
INSERT INTO partner_balance (channel_partner_id, balance_cents) VALUES (:channel_partner_id, 0) ON CONFLICT (channel_partner_id) DO NOTHING

UPDATE partner_balance SET balance_cents = balance_cents + :amount_cents WHERE channel_partner_id = :channel_partner_id
INSERT INTO partner_balance_ledger (channel_partner_id, event_type, amount_cents, source_snapshot_id, created_at)
  VALUES (:channel_partner_id, 'settlement_added', +:amount_cents, :snapshot_id, NOW())
COMMIT
```

#### `async def debit_partner_balance(channel_partner_id: int, amount_cents: int, event_type: str, metadata: dict) -> None`

`event_type` is `'clawback'` or `'payout'`. `metadata` goes into a JSONB column if the schema has one; otherwise log it.

```sql
BEGIN TRANSACTION
UPDATE partner_balance SET balance_cents = balance_cents - :amount_cents WHERE channel_partner_id = :channel_partner_id
INSERT INTO partner_balance_ledger (channel_partner_id, event_type, amount_cents, metadata_jsonb, created_at)
  VALUES (:channel_partner_id, :event_type, -:amount_cents, :metadata, NOW())
COMMIT
```

Note: balance can go negative (carry forward). No floor.

#### `async def create_payout(channel_partner_id: int, billing_run_id: int, snapshot_id: int) -> int | None`

```sql
SELECT balance_cents FROM partner_balance WHERE channel_partner_id = :channel_partner_id
```

If `balance_cents <= 0`: return None (no payout this period; balance carries).

If `balance_cents > 0`:
```sql
INSERT INTO agent_payouts (
  payee_type, payee_id, billing_run_id, snapshot_id, amount,
  currency, status, period_start, period_end, created_at
) VALUES (
  'channel_partner', :channel_partner_id, :billing_run_id, :snapshot_id, :balance_cents / 100.0,
  'USD', 'pending', :period_start, :period_end, NOW()
)
RETURNING id
```

(Note: `agent_payouts.amount` is NUMERIC(12,2) dollars per migration 019; convert from cents.)

Return the new `agent_payouts.id`. Status stays `'pending'` until an admin approves.

#### `async def approve_payout(payout_id: int, approved_by: str) -> None`

Admin action. Reads the payout, transitions status to `'approved'`, triggers `execute_payout`.

```sql
UPDATE agent_payouts SET status = 'approved', approved_by = :approved_by, approved_at = NOW()
  WHERE id = :payout_id AND status = 'pending'
```

If no row updated (already non-pending), raise `PayoutNotPendingError`. Then:
```python
await execute_payout(payout_id)
```

#### `async def execute_payout(payout_id: int) -> None`

```python
# Read the payout, look up partner's connect_account_id
row = SELECT ap.*, cp.connect_account_id FROM agent_payouts ap
        JOIN channel_partners cp ON cp.id = ap.payee_id
       WHERE ap.id = :payout_id AND ap.payee_type = 'channel_partner'

if not row.connect_account_id:
    raise PayoutMissingConnectAccountError

try:
    transfer = await asyncio.to_thread(
        stripe_client.v1.transfers.create,
        params={
            "amount": int(row.amount * 100),
            "currency": "usd",
            "destination": row.connect_account_id,
            "transfer_group": f"billing_run_{row.billing_run_id}",
            "metadata": {
                "payout_id": str(payout_id),
                "channel_partner_id": str(row.payee_id),
                "billing_run_id": str(row.billing_run_id),
            },
        },
    )

    # On success: debit balance + mark paid
    await debit_partner_balance(row.payee_id, int(row.amount * 100), event_type='payout', metadata={"transfer_id": transfer.id, "payout_id": payout_id})
    UPDATE agent_payouts SET status='paid', external_id=transfer.id, confirmed_at=NOW()
      WHERE id = :payout_id

except stripe.error.StripeError as e:
    # On failure: mark failed, DO NOT debit balance (balance must reflect what's actually owed)
    UPDATE agent_payouts SET status='failed', error_message=str(e) WHERE id = :payout_id
    raise
```

### Part 2 — `tests/test_clock_harness.py`

Pytest test harness using Stripe Test Clock for end-to-end billing cycle simulation.

Required fixtures and tests:

#### Fixture `stripe_test_clock`

```python
@pytest.fixture
async def stripe_test_clock():
    clock = await asyncio.to_thread(
        stripe_client.v1.test_helpers.test_clocks.create,
        params={"frozen_time": int(time.time())}
    )
    yield clock
    await asyncio.to_thread(stripe_client.v1.test_helpers.test_clocks.delete, clock.id)
```

#### Helper `advance_clock(test_clock_id, to_timestamp)`

```python
async def advance_clock(test_clock_id: str, to_timestamp: int) -> None:
    await asyncio.to_thread(
        stripe_client.v1.test_helpers.test_clocks.advance,
        test_clock_id,
        params={"frozen_time": to_timestamp},
    )
    # Poll until status == 'ready'
    while True:
        clock = await asyncio.to_thread(stripe_client.v1.test_helpers.test_clocks.retrieve, test_clock_id)
        if clock.status == 'ready':
            return
        if clock.status == 'internal_failure':
            raise RuntimeError(f"Test clock advance failed: {clock.id}")
        await asyncio.sleep(0.5)
```

#### Test `test_full_billing_cycle(stripe_test_clock)`

1. Create a test Customer on the clock.
2. Create a Subscription on the clock with a known Price.
3. Advance the clock to one day before next billing date.
4. Manually invoke `run_billing_cycle(period_start, period_end)`.
5. Assert: a draft `invoices` row exists, with N `billing_run_items` matching the GMV rollups.
6. Simulate a merchant filing a dispute (call `handle_dispute(...)` directly).
7. Assert: original `billing_run_items` row voided, replacement created.
8. Advance clock 5 days.
9. Call `finalize_invoice(stripe_invoice_id)`.
10. Wait for/mock `invoice.finalized` webhook → assert local `invoices.status='finalized'`.
11. Mock `invoice.paid` webhook (test clock auto-pays via attached test card).
12. Run `run_settlement(billing_run_id)`. Assert: `settlement_snapshots` row created, `partner_balance_ledger` row created, `agent_payouts` row in status 'pending'.

#### Test `test_failure_modes(stripe_test_clock)`

Exercise the 7 failure modes from v1.3 Week 9 notes:
1. Stripe SDK timeout mid-invoice generation → partial state, retry safe (idempotency_key).
2. Webhook signature mismatch → 400, no stripe_events row created.
3. Duplicate webhook event (same event_id) → 200 OK, no double-processing.
4. Invoice finalize on already-finalized invoice → Stripe error, local status stays 'finalizing' → ops alert.
5. Payout on partner with no `connect_account_id` → `PayoutMissingConnectAccountError`, no Stripe call.
6. Transfer failure (insufficient platform balance) → `agent_payouts.status='failed'`, balance NOT debited.
7. Brand churn within 90 days → clawback ledger debit, balance reduced, next payout reflects reduction.

## Style requirements

- `stripe.StripeClient` instantiated at module level using `settings.stripe_secret_key`.
- All Stripe SDK calls wrapped with `asyncio.to_thread(...)`.
- Custom exceptions: `PayoutNotPendingError`, `PayoutMissingConnectAccountError`, `SettlementAlreadyExistsError`.
- Type hints + docstrings on every public function.
- Match the import + database pattern from `services/invoice_generation_service.py` (T7).

## Acceptance criteria

- `services/partner_settlement_service.py` created with all 7 public functions.
- **Clawback uses `partner_balance_ledger` debit, NOT `stripe.Transfer.create_reversal`.** Verify no occurrence of `create_reversal` or `Transfer.reverse` in the file.
- Payout only created when `partner_balance.balance_cents > 0`; otherwise carries to next period (no row inserted).
- `settlement_snapshots` writes go through INSERT only (no UPDATE), respecting the append-only trigger from migration 114.
- Payouts require admin approval — `create_payout` inserts as `status='pending'`; only `approve_payout` advances to `'approved'`.
- `execute_payout` does NOT debit balance on Stripe failure.
- `tests/test_clock_harness.py` runs to completion using Stripe Test Clock and demonstrates all 7 failure modes.

## Don't do

- Do NOT use `stripe.Transfer.create_reversal` as the clawback mechanism. The ledger debit IS the clawback.
- Do NOT auto-approve payouts in v1. Admin approval required.
- Do NOT modify the `settlement_snapshots` table to allow UPDATE. The trigger from migration 114 enforces immutability — work with it.
- Do NOT use merchant PSP credentials. Use platform `settings.stripe_secret_key` for `stripe.Transfer.create`.
- Do NOT debit `partner_balance` on a failed Transfer. The balance must reflect what's actually owed.
- Do NOT compute settlement before `run_billing_cycle` (T7) completes — the nonpayment rule requires `invoices.status='paid'`, which depends on T7's invoice generation + webhook flow.
