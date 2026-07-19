from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

from db.database import database

logger = logging.getLogger(__name__)

PROMO_TAKE_RATE_BP = 500
STANDARD_TAKE_RATE_BP = 1000
PROMO_CACHE_TTL_SECONDS = 300

_promo_cache: dict[str, tuple[datetime | None, float]] = {}


# DATE(timestamptz) and (timestamptz)::date both read session timezone.
# Bucketing must be UTC to match billing-period DATE columns (migrations
# 120/121) — otherwise an order created at 23:30 UTC drifts to the next
# calendar day under a Tokyo/Shanghai session and double-bills.
_ROLLUP_QUERY = """
SELECT
    (e.created_at AT TIME ZONE 'UTC')::date AS date,
    e.merchant_id,
    e.agent_id,
    e.channel_partner_id,
    SUM(e.gross_attributed_gmv_cents) AS gross_sum,
    SUM(COALESCE(e.refund_amount_cents, 0)) AS refund_sum
FROM commerce_attribution_edges e
WHERE (e.created_at AT TIME ZONE 'UTC')::date = :date
  AND (CAST(:merchant_id AS TEXT) IS NULL OR e.merchant_id = CAST(:merchant_id AS TEXT))
  AND e.gross_attributed_gmv_cents IS NOT NULL
  -- Exclude fallback-INFERRED edges (#1481): token-less recoveries are recorded for
  -- coverage but never billed. NULL-safe (real edges lack the key → counted).
  AND (e.metadata->>'inferred')::boolean IS NOT TRUE
GROUP BY (e.created_at AT TIME ZONE 'UTC')::date, e.merchant_id, e.agent_id, e.channel_partner_id
"""


_UPSERT_ROLLUP_QUERY = """
INSERT INTO gmv_attribution_daily
  (date, merchant_id, agent_id, channel_partner_id,
   gross_attributed_gmv_cents, refund_amount_cents, net_attributed_gmv_cents,
   take_rate_bp, take_amount_cents, updated_at)
VALUES
  (:date, :merchant_id, :agent_id, :channel_partner_id,
   :gross_attributed_gmv_cents, :refund_amount_cents, :net_attributed_gmv_cents,
   :take_rate_bp, :take_amount_cents, NOW())
ON CONFLICT (date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))
DO UPDATE SET
  gross_attributed_gmv_cents = EXCLUDED.gross_attributed_gmv_cents,
  refund_amount_cents = EXCLUDED.refund_amount_cents,
  net_attributed_gmv_cents = EXCLUDED.net_attributed_gmv_cents,
  take_rate_bp = EXCLUDED.take_rate_bp,
  take_amount_cents = EXCLUDED.take_amount_cents,
  updated_at = NOW()
"""


# `merchants.id` is an integer PK, while commerce rollups use the operational
# string ID. The monetization bridge is merchants.subscription_id ->
# user_subscriptions.id, where user_subscriptions.merchant_id stores that string.
_PROMO_LOOKUP_QUERY = """
SELECT m.promo_period_until
FROM merchants m
JOIN user_subscriptions us ON us.id = m.subscription_id
WHERE us.merchant_id = :merchant_id
ORDER BY us.updated_at DESC NULLS LAST, us.created_at DESC NULLS LAST
LIMIT 1
"""


_EDGE_FOR_UPDATE_QUERY = """
SELECT edge_id, merchant_id, created_at
FROM commerce_attribution_edges
WHERE edge_id = :edge_id
FOR UPDATE
"""


_APPLY_REFUND_QUERY = """
UPDATE commerce_attribution_edges
SET
    refund_amount_cents = COALESCE(refund_amount_cents, 0) + :refund_amount_cents,
    refunded_at = COALESCE(refunded_at, NOW()),
    updated_at = NOW()
WHERE edge_id = :edge_id
"""


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _coerce_date(value: Any) -> date:
    # Bucket in UTC to match the (created_at AT TIME ZONE 'UTC')::date used by
    # _ROLLUP_QUERY. apply_refund() reuses this to pick the rollup day for
    # recompute_for_date(); if they drift, the recompute targets the wrong day.
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .date()
        )
    raise ValueError(f"Cannot derive date from value {value!r}")


def _promo_is_active(promo_period_until: datetime | None) -> bool:
    if promo_period_until is None:
        return False

    if promo_period_until.tzinfo is None:
        promo_period_until = promo_period_until.replace(tzinfo=timezone.utc)

    return promo_period_until > datetime.now(timezone.utc)


async def _fetch_promo_period_until(merchant_id: str) -> datetime | None:
    cached = _promo_cache.get(merchant_id)
    now_epoch = time.time()
    if cached and now_epoch - cached[1] < PROMO_CACHE_TTL_SECONDS:
        return cached[0]

    row = await database.fetch_one(_PROMO_LOOKUP_QUERY, {"merchant_id": merchant_id})
    promo_period_until = _get(row, "promo_period_until") if row else None
    _promo_cache[merchant_id] = (promo_period_until, now_epoch)
    return promo_period_until


async def _take_rate_bp_for_merchant(merchant_id: str) -> int:
    promo_period_until = await _fetch_promo_period_until(merchant_id)
    if _promo_is_active(promo_period_until):
        return PROMO_TAKE_RATE_BP
    return STANDARD_TAKE_RATE_BP


async def _aggregate_for_date(target_date: date, merchant_id: Optional[str] = None) -> int:
    async with database.transaction():
        rows = await database.fetch_all(
            _ROLLUP_QUERY,
            {"date": target_date, "merchant_id": merchant_id},
        )

        for row in rows:
            gross_sum = _as_int(_get(row, "gross_sum"))
            refund_sum = _as_int(_get(row, "refund_sum"))
            net = max(gross_sum - refund_sum, 0)
            row_merchant_id = str(_get(row, "merchant_id"))
            take_rate_bp = await _take_rate_bp_for_merchant(row_merchant_id)
            take_amount_cents = net * take_rate_bp // 10000

            await database.execute(
                _UPSERT_ROLLUP_QUERY,
                {
                    "date": _get(row, "date") or target_date,
                    "merchant_id": row_merchant_id,
                    "agent_id": _get(row, "agent_id"),
                    "channel_partner_id": _get(row, "channel_partner_id"),
                    "gross_attributed_gmv_cents": gross_sum,
                    "refund_amount_cents": refund_sum,
                    "net_attributed_gmv_cents": net,
                    "take_rate_bp": take_rate_bp,
                    "take_amount_cents": take_amount_cents,
                },
            )

    return len(rows)


async def aggregate_daily(date: date) -> int:
    """Aggregate one day's attributed gross/refund/net GMV into daily rollups.

    The upstream gross edge amount is the v1.3 GMV basis:
    orders.subtotal - orders.discount_total. Tax, shipping, and orders.total are
    deliberately excluded from this service.
    """
    return await _aggregate_for_date(date)


async def recompute_for_date(date: date, merchant_id: str) -> None:
    """Recompute daily GMV attribution rollups for one merchant and date."""
    await _aggregate_for_date(date, merchant_id=merchant_id)


async def apply_refund(edge_id: str, refund_amount_cents: int) -> None:
    """Apply an attributed refund to one edge, then recompute its daily rollup."""
    if refund_amount_cents < 0:
        raise ValueError("refund_amount_cents must be non-negative")

    async with database.transaction():
        edge = await database.fetch_one(_EDGE_FOR_UPDATE_QUERY, {"edge_id": edge_id})
        if not edge:
            raise ValueError(f"Attribution edge not found: {edge_id}")

        await database.execute(
            _APPLY_REFUND_QUERY,
            {
                "edge_id": edge_id,
                "refund_amount_cents": refund_amount_cents,
            },
        )

    await recompute_for_date(
        date=_coerce_date(_get(edge, "created_at")),
        merchant_id=str(_get(edge, "merchant_id")),
    )
