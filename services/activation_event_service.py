from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from db.database import database


@dataclass(frozen=True)
class ActivationDecision:
    eligible: bool
    activation_date: date | None
    reason: str
    qualifying_invoice_id: int | None
    net_cash_received_cents: int
    metadata: dict[str, Any]


async def evaluate_activation(*, merchant_id: str) -> ActivationDecision:
    """Evaluate whether a brand has reached the Track B activation event.

    Activation date is the date the first eligible local Pivota invoice reaches
    status='paid' with net cash received greater than zero. Eligible invoices in
    this pragmatic PR scope are local invoices where status='paid',
    total_cents > 0, and total_cents - refunded_cents > 0.

    Not eligible: unpaid/non-paid invoices, zero-dollar invoices, and invoices
    fully refunded down to net zero. Partial refunds keep the invoice eligible
    when the remaining net cash is positive.

    Full-refund edge case: this evaluator re-queries the local ledger each time,
    so a fully refunded invoice may stop qualifying and a later paid invoice may
    become the first eligible result. try_activate_brand() is intentionally
    WRITE-ONCE, so a later full refund of the qualifying invoice does not clear
    or move partner_attribution.activated_at after it has been written.

    Customer-credit-covered invoices and manual wires/checks currently count if
    the local invoice is marked paid; tightening that requires source/payment
    typing not present in the current invoice schema.
    """

    qualifying_invoice_row = await database.fetch_one(
        """
        SELECT
          id,
          stripe_invoice_id,
          total_cents,
          refunded_cents,
          paid_at,
          finalized_at,
          created_at,
          status
        FROM invoices
        WHERE merchant_id = :merchant_id
          AND status = 'paid'
          AND total_cents > 0
          AND (total_cents - refunded_cents) > 0
        ORDER BY COALESCE(paid_at, finalized_at, created_at) ASC
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if not qualifying_invoice_row:
        return ActivationDecision(
            eligible=False,
            activation_date=None,
            reason="No paid positive-net Pivota invoice found",
            qualifying_invoice_id=None,
            net_cash_received_cents=0,
            metadata={
                "merchant_id": merchant_id,
                "eligible_invoice_scope": "paid_positive_net_local_invoices",
            },
        )

    activation_timestamp = _row_get(qualifying_invoice_row, "paid_at") or _row_get(
        qualifying_invoice_row,
        "finalized_at",
    )
    if activation_timestamp is None:
        return ActivationDecision(
            eligible=False,
            activation_date=None,
            reason="First paid positive-net invoice is missing paid_at/finalized_at",
            qualifying_invoice_id=_optional_int(_row_get(qualifying_invoice_row, "id")),
            net_cash_received_cents=0,
            metadata={
                "merchant_id": merchant_id,
                "stripe_invoice_id": _row_get(
                    qualifying_invoice_row,
                    "stripe_invoice_id",
                ),
            },
        )

    activation_date = _as_date(activation_timestamp)
    net_cash_received_cents = await _net_cash_received_through_activation_date(
        merchant_id=merchant_id,
        activation_date=activation_date,
    )

    total_raw_cents = _as_int(_row_get(qualifying_invoice_row, "total_cents"))
    refunded_raw_cents = _as_int(_row_get(qualifying_invoice_row, "refunded_cents"))
    # RAW - RAW: qualifying invoice net cash retained after refunds.
    qualifying_invoice_net_raw_cents = total_raw_cents - refunded_raw_cents

    return ActivationDecision(
        eligible=True,
        activation_date=activation_date,
        reason="First paid positive-net Pivota invoice qualifies for activation",
        qualifying_invoice_id=_optional_int(_row_get(qualifying_invoice_row, "id")),
        net_cash_received_cents=net_cash_received_cents,
        metadata={
            "merchant_id": merchant_id,
            "stripe_invoice_id": _row_get(qualifying_invoice_row, "stripe_invoice_id"),
            "qualifying_invoice_total_raw_cents": total_raw_cents,
            "qualifying_invoice_refunded_raw_cents": refunded_raw_cents,
            "qualifying_invoice_net_raw_cents": qualifying_invoice_net_raw_cents,
            "eligible_invoice_scope": "paid_positive_net_local_invoices",
            "activation_timestamp": activation_timestamp.isoformat()
            if hasattr(activation_timestamp, "isoformat")
            else str(activation_timestamp),
        },
    )


async def try_activate_brand(
    *,
    merchant_id: str,
    channel_partner_id: int,
) -> ActivationDecision:
    """Write partner_attribution.activated_at once after eligibility passes.

    The update is guarded by SELECT FOR UPDATE plus activated_at IS NULL on the
    UPDATE itself. If activated_at is already set, this function is a no-op even
    if evaluate_activation() would currently return a different date.
    """

    decision = await evaluate_activation(merchant_id=merchant_id)
    if not decision.eligible or decision.activation_date is None:
        return decision

    async with database.transaction():
        attribution_row = await database.fetch_one(
            """
            SELECT id, activated_at
            FROM partner_attribution
            WHERE merchant_id = :merchant_id
              AND channel_partner_id = :channel_partner_id
            LIMIT 1
            FOR UPDATE
            """,
            {
                "merchant_id": merchant_id,
                "channel_partner_id": channel_partner_id,
            },
        )
        if not attribution_row:
            return decision
        if _row_get(attribution_row, "activated_at") is not None:
            return decision

        await database.execute(
            """
            UPDATE partner_attribution
            SET activated_at = :activated_at
            WHERE id = :attribution_id
              AND activated_at IS NULL
            """,
            {
                "activated_at": decision.activation_date,
                "attribution_id": _row_get(attribution_row, "id"),
            },
        )

    return decision


async def _net_cash_received_through_activation_date(
    *,
    merchant_id: str,
    activation_date: date,
) -> int:
    row = await database.fetch_one(
        """
        SELECT COALESCE(SUM(total_cents - refunded_cents), 0) AS net_cash_received_cents
        FROM invoices
        WHERE merchant_id = :merchant_id
          AND status = 'paid'
          AND total_cents > 0
          AND (total_cents - refunded_cents) > 0
          AND CAST(COALESCE(paid_at, finalized_at) AS DATE) <= :activation_date
        """,
        {
            "merchant_id": merchant_id,
            "activation_date": activation_date,
        },
    )
    return _as_int(_row_get(row, "net_cash_received_cents"))


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key)
