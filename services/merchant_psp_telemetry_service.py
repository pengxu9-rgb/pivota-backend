"""Merchant PSP telemetry aggregation.

Metrics are reported only when a PSP has attributed runtime payment data.
Missing telemetry stays unavailable instead of being coerced to 0 or a
synthetic success rate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

from db.database import database

logger = logging.getLogger(__name__)

PAYMENT_TELEMETRY_NOT_REPORTED = "Payment telemetry not reported"
PAYMENT_TELEMETRY_WINDOW = "utc_day"
SUCCESS_ATTEMPT_STATUSES = ("success", "succeeded")
SUCCESS_ORDER_STATUSES = ("paid", "completed", "succeeded")


def _today_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def unavailable_payment_telemetry() -> Dict[str, Any]:
    return {
        "payment_telemetry_reported": False,
        "payment_telemetry_source": None,
        "payment_telemetry_window": PAYMENT_TELEMETRY_WINDOW,
        "message": PAYMENT_TELEMETRY_NOT_REPORTED,
    }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _as_float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _reported_telemetry(*, total_count: Any, success_count: Any, volume: Any, source: str) -> Dict[str, Any]:
    total = _as_int(total_count)
    successful = _as_int(success_count)
    total_volume = _as_float(volume)
    if total <= 0:
        return unavailable_payment_telemetry()

    return {
        "payment_telemetry_reported": True,
        "payment_telemetry_source": source,
        "payment_telemetry_window": PAYMENT_TELEMETRY_WINDOW,
        "success_rate": round((successful / total) * 100, 1),
        "volume_today": round(total_volume, 2),
        "transaction_count": total,
    }


def _normalize_rows(rows: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        row_dict = dict(row)
        psp_id = str(row_dict.get("psp_id") or "").strip()
        if psp_id:
            normalized[psp_id] = row_dict
    return normalized


async def get_merchant_psp_telemetry(
    merchant_id: str,
    *,
    psp_id: Optional[str] = None,
    start_time: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return trusted PSP telemetry keyed by psp_id.

    Primary source is payment_attempts joined through orders for merchant
    ownership. Orders fallback only reports when an order has explicit PSP
    attribution for the same UTC day.
    """

    merchant_id = str(merchant_id or "").strip()
    if not merchant_id:
        return {}

    start = start_time or _today_start_utc()
    psp_filter = "AND mp.psp_id = :psp_id" if psp_id else ""
    params: Dict[str, Any] = {"merchant_id": merchant_id, "start_time": start}
    if psp_id:
        params["psp_id"] = psp_id

    attempt_rows: Dict[str, Dict[str, Any]] = {}
    try:
        attempt_query = f"""
            SELECT
                mp.psp_id,
                mp.provider,
                COUNT(pa.attempt_id) AS total_count,
                COUNT(pa.attempt_id) FILTER (
                    WHERE LOWER(pa.status) IN ('success', 'succeeded')
                ) AS success_count,
                COALESCE(
                    SUM(
                        CASE
                            WHEN LOWER(pa.status) IN ('success', 'succeeded')
                            THEN pa.amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_volume
            FROM merchant_psps mp
            LEFT JOIN orders o ON o.merchant_id = mp.merchant_id
                AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
            LEFT JOIN payment_attempts pa ON pa.order_id = o.order_id
                AND pa.created_at >= :start_time
                AND (
                    LOWER(pa.psp_name) = LOWER(mp.psp_id)
                    OR (
                        LOWER(pa.psp_name) = LOWER(mp.provider)
                        AND (o.psp_id IS NULL OR o.psp_id = mp.psp_id)
                    )
                )
            WHERE mp.merchant_id = :merchant_id
            {psp_filter}
            GROUP BY mp.psp_id, mp.provider
        """
        attempt_rows = _normalize_rows(await database.fetch_all(attempt_query, params))
    except Exception as exc:
        logger.info("Payment attempt telemetry unavailable for merchant %s: %s", merchant_id, exc)

    telemetry: Dict[str, Dict[str, Any]] = {}
    missing_psp_ids = []
    for row_psp_id, row in attempt_rows.items():
        metric = _reported_telemetry(
            total_count=row.get("total_count"),
            success_count=row.get("success_count"),
            volume=row.get("total_volume"),
            source="payment_attempts",
        )
        telemetry[row_psp_id] = metric
        if not metric.get("payment_telemetry_reported"):
            missing_psp_ids.append(row_psp_id)

    # Fallback for legacy orders that have explicit PSP attribution but no
    # payment_attempts rows. This is still real attributed data, not a default.
    if missing_psp_ids or not attempt_rows:
        try:
            order_query = f"""
                SELECT
                    mp.psp_id,
                    mp.provider,
                    COUNT(o.order_id) AS total_count,
                    COUNT(o.order_id) FILTER (
                        WHERE LOWER(o.payment_status) IN ('paid', 'completed', 'succeeded')
                    ) AS success_count,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN LOWER(o.payment_status) IN ('paid', 'completed', 'succeeded')
                                THEN o.total
                                ELSE 0
                            END
                        ),
                        0
                    ) AS total_volume
                FROM merchant_psps mp
                LEFT JOIN orders o ON o.merchant_id = mp.merchant_id
                    AND o.created_at >= :start_time
                    AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
                    AND (
                        (o.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
                        OR (
                            o.psp_id IS NULL
                            AND o.psp_used IS NOT NULL
                            AND (
                                LOWER(o.psp_used) = LOWER(mp.provider)
                                OR LOWER(o.psp_used) = LOWER(mp.psp_id)
                            )
                        )
                    )
                WHERE mp.merchant_id = :merchant_id
                {psp_filter}
                GROUP BY mp.psp_id, mp.provider
            """
            for row in await database.fetch_all(order_query, params):
                row_dict = dict(row)
                row_psp_id = str(row_dict.get("psp_id") or "").strip()
                if not row_psp_id:
                    continue
                if telemetry.get(row_psp_id, {}).get("payment_telemetry_reported"):
                    continue
                metric = _reported_telemetry(
                    total_count=row_dict.get("total_count"),
                    success_count=row_dict.get("success_count"),
                    volume=row_dict.get("total_volume"),
                    source="orders",
                )
                if metric.get("payment_telemetry_reported"):
                    telemetry[row_psp_id] = metric
                else:
                    telemetry.setdefault(row_psp_id, unavailable_payment_telemetry())
        except Exception as exc:
            logger.info("Order PSP telemetry unavailable for merchant %s: %s", merchant_id, exc)

    return telemetry
