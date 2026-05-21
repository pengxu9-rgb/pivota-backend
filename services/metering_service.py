from __future__ import annotations

from decimal import Decimal
import json
import os
import time
from typing import Any, Mapping, Optional

from db.database import database
from services.merchant_payment_initiation_service import initiate_merchant_payment


class InsufficientCreditsError(Exception):
    pass


class ReservationNotFound(Exception):
    pass


class ReservationAlreadyFinalized(Exception):
    pass


class AutoTopupCapExceeded(Exception):
    pass


_COST_CACHE_TTL_SECONDS = 60
_DEFAULT_AUTO_TOPUP_DAILY_CAP = 3
_cost_cache: dict[str, tuple[int, float]] = {}


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _json_payload(value: Optional[dict[str, Any]]) -> str:
    return json.dumps(value or {}, default=str)


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except Exception:
        return default


async def _get_operation_cost(operation_type: str) -> int:
    now = time.time()
    cached = _cost_cache.get(operation_type)
    if cached and now - cached[1] < _COST_CACHE_TTL_SECONDS:
        return cached[0]

    row = await database.fetch_one(
        """
        SELECT base_cost_credits AS credits
        FROM operation_cost_config
        WHERE operation_type = :operation_type
          AND status = 'active'
          AND effective_from <= NOW()
          AND (effective_until IS NULL OR effective_until > NOW())
        ORDER BY version DESC
        LIMIT 1
        """,
        {"operation_type": operation_type},
    )
    if not row:
        raise ValueError(f"Unknown operation_type: {operation_type}")

    cost = int(_row_value(row, "credits", 0) or 0)
    _cost_cache[operation_type] = (cost, now)
    return cost


async def _get_merchant_for_update(merchant_id: str) -> Optional[Mapping[str, Any]]:
    return await database.fetch_one(
        """
        SELECT
          merchant_id,
          balance,
          auto_topup_enabled,
          auto_topup_threshold,
          auto_topup_amount_credits
        FROM merchant_credits
        WHERE merchant_id = :merchant_id
        FOR UPDATE
        """,
        {"merchant_id": merchant_id},
    )


async def _get_reservation_for_update(reservation_id: str) -> Optional[Mapping[str, Any]]:
    return await database.fetch_one(
        """
        SELECT
          id,
          merchant_id,
          operation_type,
          operation_id,
          credits_held,
          status
        FROM credit_reservations
        WHERE id = :reservation_id
        FOR UPDATE
        """,
        {"reservation_id": reservation_id},
    )


async def _auto_topup_allowed(merchant_id: str) -> bool:
    daily_cap = _env_int("METERING_AUTO_TOPUP_DAILY_CAP", _DEFAULT_AUTO_TOPUP_DAILY_CAP)
    monthly_ceiling_cents = _env_int("METERING_AUTO_TOPUP_MONTHLY_SPEND_CEILING_CENTS", 0)

    row = await database.fetch_one(
        """
        SELECT
          COUNT(*) FILTER (
            WHERE occurred_at >= DATE_TRUNC('day', NOW())
          ) AS daily_topup_count,
          COALESCE(
            SUM(credits_delta) FILTER (
              WHERE occurred_at >= DATE_TRUNC('month', NOW())
            ),
            0
          ) AS monthly_spend_cents
        FROM credit_ledger
        WHERE merchant_id = :merchant_id
          AND operation_type = 'auto_topup'
        """,
        {"merchant_id": merchant_id},
    )
    daily_topup_count = int(_row_value(row or {}, "daily_topup_count", 0) or 0)
    monthly_spend_cents = int(_row_value(row or {}, "monthly_spend_cents", 0) or 0)

    if daily_cap >= 0 and daily_topup_count >= daily_cap:
        return False
    if monthly_ceiling_cents > 0 and monthly_spend_cents >= monthly_ceiling_cents:
        return False
    return True


async def _current_balance_for_update(merchant_id: str) -> int:
    row = await database.fetch_one(
        """
        SELECT balance
        FROM merchant_credits
        WHERE merchant_id = :merchant_id
        FOR UPDATE
        """,
        {"merchant_id": merchant_id},
    )
    if not row:
        raise ValueError(f"merchant_credits row not found for merchant_id={merchant_id}")
    return int(_row_value(row, "balance", 0) or 0)


async def _add_merchant_balance(merchant_id: str, credits_delta: int) -> int:
    row = await database.fetch_one(
        """
        UPDATE merchant_credits
        SET balance = balance + :credits_delta
        WHERE merchant_id = :merchant_id
        RETURNING balance
        """,
        {"merchant_id": merchant_id, "credits_delta": credits_delta},
    )
    if not row:
        raise ValueError(f"merchant_credits row not found for merchant_id={merchant_id}")
    return int(_row_value(row, "balance", 0) or 0)


async def _insert_ledger(
    *,
    merchant_id: str,
    operation_type: str,
    credits_delta: int,
    balance_after: int,
    operation_id: Optional[str] = None,
    reservation_id: Optional[str] = None,
    source_payment_intent_id: Optional[str] = None,
    source_type: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    await database.execute(
        """
        INSERT INTO credit_ledger (
          merchant_id,
          operation_type,
          operation_id,
          credits_delta,
          balance_after,
          reservation_id,
          source_payment_intent_id,
          source_type,
          metadata
        ) VALUES (
          :merchant_id,
          :operation_type,
          :operation_id,
          :credits_delta,
          :balance_after,
          :reservation_id,
          :source_payment_intent_id,
          :source_type,
          :metadata
        )
        """,
        {
            "merchant_id": merchant_id,
            "operation_type": operation_type,
            "operation_id": operation_id,
            "credits_delta": credits_delta,
            "balance_after": balance_after,
            "reservation_id": reservation_id,
            "source_payment_intent_id": source_payment_intent_id,
            "source_type": source_type,
            "metadata": _json_payload(metadata),
        },
    )


async def reserve(
    merchant_id: str,
    operation_type: str,
    operation_id: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Reserve credits for an operation and return the reservation id."""
    async with database.transaction():
        merchant = await _get_merchant_for_update(merchant_id)
        cost = await _get_operation_cost(operation_type)
        balance = int(_row_value(merchant or {}, "balance", 0) or 0)
        if not merchant or balance < cost:
            if merchant and bool(_row_value(merchant, "auto_topup_enabled", False)):
                if await _auto_topup_allowed(merchant_id):
                    await _trigger_auto_topup(merchant_id, cost)
                    raise AutoTopupCapExceeded("auto_topup_triggered")
            raise InsufficientCreditsError(f"balance={balance}, cost={cost}")

        row = await database.fetch_one(
            """
            INSERT INTO credit_reservations (
              merchant_id,
              operation_type,
              operation_id,
              credits_held,
              status,
              expires_at,
              metadata
            ) VALUES (
              :merchant_id,
              :operation_type,
              :operation_id,
              :credits_held,
              'reserved',
              NOW() + INTERVAL '15 minutes',
              :metadata
            )
            RETURNING id
            """,
            {
                "merchant_id": merchant_id,
                "operation_type": operation_type,
                "operation_id": operation_id,
                "credits_held": cost,
                "metadata": _json_payload(metadata),
            },
        )
        await database.execute(
            """
            UPDATE merchant_credits
            SET balance = balance - :credits_held
            WHERE merchant_id = :merchant_id
            """,
            {"merchant_id": merchant_id, "credits_held": cost},
        )
        return str(_row_value(row or {}, "id", ""))


async def commit(reservation_id: str) -> None:
    """Commit a reserved operation and append the final debit ledger row."""
    async with database.transaction():
        reservation = await _get_reservation_for_update(reservation_id)
        if not reservation:
            raise ReservationNotFound
        status = str(_row_value(reservation, "status", ""))
        if status != "reserved":
            raise ReservationAlreadyFinalized(f"status={status}")

        merchant_id = str(_row_value(reservation, "merchant_id", ""))
        balance_after = await _current_balance_for_update(merchant_id)
        await database.execute(
            """
            UPDATE credit_reservations
            SET status = 'committed',
                finalized_at = NOW()
            WHERE id = :reservation_id
            """,
            {"reservation_id": reservation_id},
        )
        await _insert_ledger(
            merchant_id=merchant_id,
            operation_type=str(_row_value(reservation, "operation_type", "")),
            operation_id=str(_row_value(reservation, "operation_id", "")),
            credits_delta=-int(_row_value(reservation, "credits_held", 0) or 0),
            balance_after=balance_after,
            reservation_id=reservation_id,
            source_type="operation_commit",
        )


async def release(reservation_id: str) -> None:
    """Release a reservation and return its held credits to the merchant balance."""
    async with database.transaction():
        reservation = await _get_reservation_for_update(reservation_id)
        if not reservation:
            raise ReservationNotFound
        status = str(_row_value(reservation, "status", ""))
        if status != "reserved":
            raise ReservationAlreadyFinalized(f"status={status}")

        merchant_id = str(_row_value(reservation, "merchant_id", ""))
        credits_held = int(_row_value(reservation, "credits_held", 0) or 0)
        await _current_balance_for_update(merchant_id)
        await database.execute(
            """
            UPDATE credit_reservations
            SET status = 'released',
                finalized_at = NOW()
            WHERE id = :reservation_id
            """,
            {"reservation_id": reservation_id},
        )
        balance_after = await _add_merchant_balance(merchant_id, credits_held)
        await _insert_ledger(
            merchant_id=merchant_id,
            operation_type="release_refund",
            operation_id=str(_row_value(reservation, "operation_id", "")),
            credits_delta=credits_held,
            balance_after=balance_after,
            reservation_id=reservation_id,
            source_type="operation_release",
            metadata={
                "original_operation_type": str(_row_value(reservation, "operation_type", "")),
            },
        )


async def topup(
    merchant_id: str,
    payment_intent_id: str,
    credits_purchased: int,
    topup_type: str = "manual_topup",
) -> None:
    """Apply a confirmed topup payment to the merchant credit balance."""
    if topup_type not in {"manual_topup", "auto_topup"}:
        raise ValueError("topup_type must be 'manual_topup' or 'auto_topup'")
    if credits_purchased <= 0:
        raise ValueError("credits_purchased must be positive")

    async with database.transaction():
        await _current_balance_for_update(merchant_id)
        balance_after = await _add_merchant_balance(merchant_id, credits_purchased)
        await _insert_ledger(
            merchant_id=merchant_id,
            operation_type=topup_type,
            credits_delta=credits_purchased,
            balance_after=balance_after,
            source_payment_intent_id=payment_intent_id,
            source_type="topup",
        )


async def expire_stale_reservations() -> int:
    """Expire stale reserved holds and refund their held credits."""
    rows = await database.fetch_all(
        """
        SELECT id, merchant_id, credits_held
        FROM credit_reservations
        WHERE status = 'reserved'
          AND expires_at < NOW()
        """
    )

    expired_count = 0
    for row in rows:
        reservation_id = str(_row_value(row, "id", ""))
        async with database.transaction():
            reservation = await database.fetch_one(
                """
                SELECT
                  id,
                  merchant_id,
                  operation_type,
                  operation_id,
                  credits_held,
                  status
                FROM credit_reservations
                WHERE id = :reservation_id
                  AND status = 'reserved'
                FOR UPDATE
                """,
                {"reservation_id": reservation_id},
            )
            if not reservation:
                continue

            merchant_id = str(_row_value(reservation, "merchant_id", ""))
            credits_held = int(_row_value(reservation, "credits_held", 0) or 0)
            await _current_balance_for_update(merchant_id)
            await database.execute(
                """
                UPDATE credit_reservations
                SET status = 'expired',
                    finalized_at = NOW()
                WHERE id = :reservation_id
                """,
                {"reservation_id": reservation_id},
            )
            balance_after = await _add_merchant_balance(merchant_id, credits_held)
            await _insert_ledger(
                merchant_id=merchant_id,
                operation_type="reservation_expired",
                operation_id=str(_row_value(reservation, "operation_id", "")),
                credits_delta=credits_held,
                balance_after=balance_after,
                reservation_id=reservation_id,
                source_type="expiry",
                metadata={
                    "original_operation_type": str(_row_value(reservation, "operation_type", "")),
                },
            )
            expired_count += 1
    return expired_count


async def _trigger_auto_topup(merchant_id: str, needed_credits: int) -> None:
    """Create a one-time topup PaymentIntent through the existing PSP path."""
    row = await database.fetch_one(
        """
        SELECT auto_topup_amount_credits
        FROM merchant_credits
        WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    configured_credits = int(_row_value(row or {}, "auto_topup_amount_credits", 0) or 0)
    credits_to_purchase = max(configured_credits, int(needed_credits))
    amount = Decimal(credits_to_purchase) / Decimal("100")
    currency = str(os.getenv("METERING_TOPUP_CURRENCY") or "USD").upper()

    result = await initiate_merchant_payment(
        merchant_id=merchant_id,
        amount=amount,
        currency=currency,
        metadata={
            "topup_type": "auto_topup",
            "merchant_id": merchant_id,
            "credits_requested": needed_credits,
            "credits_to_purchase": credits_to_purchase,
            "source": "metering_service",
        },
    )
    if not result.get("success"):
        error = result.get("error_message") or "auto_topup_payment_initiation_failed"
        raise AutoTopupCapExceeded(str(error))
