# Codex prompt — T5: Draft services/metering_service.py (reserve/commit/release/topup/expire)

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Stack: Python (FastAPI), Postgres (Railway), Stripe (already integrated as PSP).
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` — implement v1.3 exactly, do not improvise on architecture.
Existing patterns to follow: see db/, services/, routes/, adapters/ — match style of existing files.
Output: code in the existing repo layout. Migrations as SQL files in db/migrations/.
Don't add new dependencies unless absolutely required. Don't rewrite existing code unless explicitly asked.

## Prerequisite inputs — read these first

1. `db/migrations/104_merchant_credits.sql` — `merchant_credits` schema (balance, auto_topup_enabled, auto_topup_threshold, auto_topup_amount_credits).
2. `db/migrations/105_credit_reservations.sql` — `credit_reservations` schema (status lifecycle: reserved → committed/released/expired; expires_at; credits_held).
3. `db/migrations/106_credit_ledger.sql` — `credit_ledger` schema (append-only audit log; operation_type; credits_delta; balance_after; source columns).
4. `db/migrations/107_operation_cost_config.sql` — `operation_cost_config` schema (versioned operation costs).
5. Read one or two existing services (e.g., `services/refund_service.py` or `services/psp_payment_finalizer.py`) to understand async/DB/error-handling conventions used in this codebase.

## Task

Create `services/metering_service.py`. This service is internal-only — it is NOT exposed via HTTP. It is called by agent dispatchers before and after running operations.

## Typed exceptions (define at top of file)

```python
class InsufficientCreditsError(Exception): ...
class ReservationNotFound(Exception): ...
class ReservationAlreadyFinalized(Exception): ...
class AutoTopupCapExceeded(Exception): ...
```

## Functions to implement

### `async def reserve(merchant_id: str, operation_type: str, operation_id: str, metadata: dict | None = None) -> str`

Returns `reservation_id`.

```
BEGIN TRANSACTION
SELECT FOR UPDATE merchant_credits WHERE merchant_id = :merchant_id
Look up operation cost: SELECT credits FROM operation_cost_config WHERE operation_type = :operation_type AND is_active = TRUE ORDER BY version DESC LIMIT 1 (cache result with TTL of 60s in a module-level dict)
IF balance < cost:
    IF auto_topup_enabled AND daily_topup_count < daily_topup_cap AND monthly_spend_cents < monthly_spend_ceiling:
        call _trigger_auto_topup(merchant_id, cost) — creates PaymentIntent via existing PSP path; returns immediately WITHOUT completing the reservation; raise AutoTopupCapExceeded with message "auto_topup_triggered" so caller knows to retry after webhook confirms topup
    ELSE:
        raise InsufficientCreditsError(f"balance={balance}, cost={cost}")
INSERT credit_reservations (merchant_id, operation_type, operation_id, credits_held=cost, status='reserved', expires_at=NOW()+INTERVAL '15 minutes', metadata=metadata)
UPDATE merchant_credits SET balance = balance - cost
COMMIT
return reservation_id (str(row.id))
```

### `async def commit(reservation_id: str) -> None`

```
BEGIN TRANSACTION
SELECT FOR UPDATE credit_reservations WHERE id = :reservation_id
IF NOT FOUND: raise ReservationNotFound
IF status != 'reserved': raise ReservationAlreadyFinalized(f"status={status}")
UPDATE credit_reservations SET status='committed', finalized_at=NOW()
INSERT credit_ledger (merchant_id, operation_type=reservation.operation_type, operation_id=reservation.operation_id, credits_delta=-reservation.credits_held, balance_after=(SELECT balance FROM merchant_credits WHERE merchant_id=reservation.merchant_id), reservation_id=reservation_id, source_type='operation_commit')
COMMIT
```

### `async def release(reservation_id: str) -> None`

```
BEGIN TRANSACTION
SELECT FOR UPDATE credit_reservations WHERE id = :reservation_id
IF NOT FOUND: raise ReservationNotFound
IF status != 'reserved': raise ReservationAlreadyFinalized(f"status={status}")
UPDATE credit_reservations SET status='released', finalized_at=NOW()
UPDATE merchant_credits SET balance = balance + credits_held WHERE merchant_id = reservation.merchant_id
INSERT credit_ledger (merchant_id, operation_type='release_refund', operation_id=reservation.operation_id, credits_delta=+reservation.credits_held, balance_after=(new balance), reservation_id=reservation_id, source_type='operation_release')
COMMIT
```

### `async def topup(merchant_id: str, payment_intent_id: str, credits_purchased: int, topup_type: str = 'manual_topup') -> None`

Called from webhook handler after PaymentIntent.succeeded confirms a topup payment.

```
BEGIN TRANSACTION
SELECT FOR UPDATE merchant_credits WHERE merchant_id = :merchant_id
UPDATE merchant_credits SET balance = balance + credits_purchased
INSERT credit_ledger (merchant_id, operation_type=topup_type, credits_delta=+credits_purchased, balance_after=(new balance), source_payment_intent_id=payment_intent_id, source_type='topup')
COMMIT
```

`topup_type` is `'manual_topup'` or `'auto_topup'`.

### `async def expire_stale_reservations() -> int`

Called by a reaper cron every 5 minutes. Returns count of expired reservations.

```
SELECT id, merchant_id, credits_held FROM credit_reservations
  WHERE status = 'reserved' AND expires_at < NOW()
FOR EACH row:
    BEGIN TRANSACTION
    SELECT FOR UPDATE credit_reservations WHERE id = :id AND status = 'reserved'  -- re-check under lock
    IF found (not already finalized by concurrent commit/release):
        UPDATE credit_reservations SET status='expired', finalized_at=NOW()
        UPDATE merchant_credits SET balance = balance + credits_held WHERE merchant_id = :merchant_id
        INSERT credit_ledger (operation_type='reservation_expired', credits_delta=+credits_held, balance_after=(new balance), reservation_id=id, source_type='expiry')
    COMMIT
return count
```

### `async def _trigger_auto_topup(merchant_id: str, needed_credits: int) -> None`

Internal. Calls the existing PSP payment path to create a PaymentIntent for the topup amount. Do NOT implement Stripe directly here — call `services/merchant_payment_initiation_service.py::initiate_merchant_payment(...)` or the equivalent existing payment creation entry point. Pass `metadata={"topup_type": "auto_topup", "merchant_id": merchant_id, "credits_requested": needed_credits}`. The webhook handler (`routes/billing_routes.py`) will call `topup(...)` when PaymentIntent.succeeded fires.

## Operation cost cache

Module-level `_cost_cache: dict[str, tuple[int, float]]` mapping `operation_type → (cost, fetched_at_epoch)`. TTL = 60 seconds. On cache miss or expiry, query `operation_cost_config`. If no row found for an operation_type, raise `ValueError(f"Unknown operation_type: {operation_type}")`.

## Acceptance criteria

- `services/metering_service.py` created with all 5 public functions + `_trigger_auto_topup`.
- `SELECT FOR UPDATE` used on both `merchant_credits` and `credit_reservations` to prevent race conditions.
- `credit_ledger` row inserted for every credit-changing operation (commit, release, topup, expire).
- All four typed exceptions defined and raised appropriately.
- Type hints + docstrings on every public function.
- Unit tests in `tests/test_metering_service.py` covering:
  - Happy path: reserve → commit
  - Happy path: reserve → release
  - reserve → expire via `expire_stale_reservations`
  - InsufficientCreditsError (balance too low, auto_topup disabled)
  - AutoTopupCapExceeded / topup triggered path
  - Race condition: two concurrent reserves against same merchant (second should fail if balance only covers one)
  - `commit` on already-committed reservation raises ReservationAlreadyFinalized
  - `topup` correctly adds to balance and inserts ledger row

## Don't do

- Do NOT implement Stripe PaymentIntent creation directly in this service — delegate to the existing PSP path.
- Do NOT expose this service via HTTP — internal use only.
- Do NOT import from `routes/` — services must not depend on route modules.
