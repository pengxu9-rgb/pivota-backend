from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

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
  -- A re-roll keeps the rate the day was first billed at. `_take_rate_bp_for_merchant` answers
  -- with the merchant's promo status NOW, so re-rating a past day after a promo ended would
  -- RAISE its take when a refund triggers the recompute (review of #2271). Only a new group row
  -- takes the current rate.
  take_rate_bp = gmv_attribution_daily.take_rate_bp,
  take_amount_cents = (EXCLUDED.net_attributed_gmv_cents * gmv_attribution_daily.take_rate_bp) / 10000,
  updated_at = NOW()
"""

# One rollup of a day at a time. _aggregate_for_date reads the edge sums and then upserts. Without
# this, a nightly roll-up and a refund's recompute of the same day can interleave, and the one that
# read first writes last with sums that miss the other's refund. Per DAY, not per merchant, because
# the nightly run covers every merchant of the day.
# CAST(CAST(:date AS DATE) AS TEXT), not CAST(:date AS TEXT): asyncpg types the parameter from its
# innermost cast and refuses a Python date for a text parameter, at bind time, after a clean PREPARE.
_DAY_LOCK_KEY = "hashtext('gmv_attribution_daily:' || CAST(CAST(:date AS DATE) AS TEXT))"
_LOCK_DAY_QUERY = "SELECT pg_advisory_xact_lock(" + _DAY_LOCK_KEY + ")"
# The same key, SHARED, taken by the invoice run on every day it bills (lock_billing_days). A
# roll-up of a day therefore either commits before the invoice run reads the day, or waits for the
# invoice to commit and then sees it.
LOCK_DAY_SHARED_QUERY = "SELECT pg_advisory_xact_lock_shared(" + _DAY_LOCK_KEY + ")"
# A refund's re-roll must not stall a webhook request behind an invoice run that holds the shared
# day locks across its Stripe calls. Only recompute_for_date sets it; the nightly roll-up waits.
_RECOMPUTE_LOCK_TIMEOUT_QUERY = "SET LOCAL lock_timeout = '5s'"

# The merchants an invoice (not void) already covers on the day. Their rollup rows are frozen:
# invoice items point at them and partner settlement reads them. Checked INSIDE the day lock, where
# it cannot race the invoice run (which reads its rows under the same lock, shared, and commits its
# invoices row before releasing it). Per merchant, not per billing run: a merchant a run has not
# invoiced yet reads the day fresh when it is, so freezing it would bill a stale take.
_INVOICED_MERCHANTS_ON_DAY_QUERY = """
SELECT DISTINCT merchant_id FROM invoices
WHERE CAST(billing_period_start AS DATE) <= CAST(:date AS DATE)
  AND CAST(billing_period_end AS DATE) >= CAST(:date AS DATE)
  AND COALESCE(status, '') <> 'void'
  AND (CAST(:merchant_id AS TEXT) IS NULL OR merchant_id = CAST(:merchant_id AS TEXT))
"""


class BilledDayCheckFailed(RuntimeError):
    """The invoice check under the day lock failed; the day was not re-rolled (fail closed)."""


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


async def _aggregate_for_date(
    target_date: date, merchant_id: Optional[str] = None, *, lock_timeout: bool = False,
) -> dict[str, Any]:
    """Roll one day up under its exclusive lock, leaving every INVOICED merchant's rows as billed.

    Returns {"written": rows upserted, "invoiced_merchants": merchants left as billed}. Raises
    BilledDayCheckFailed if the invoice check fails (the transaction is aborted, nothing written).
    """
    written = 0
    async with database.transaction():
        if lock_timeout:
            await database.execute(_RECOMPUTE_LOCK_TIMEOUT_QUERY)
        await database.execute(_LOCK_DAY_QUERY, {"date": target_date})
        try:
            invoiced = {
                str(_get(r, "merchant_id"))
                for r in await database.fetch_all(
                    _INVOICED_MERCHANTS_ON_DAY_QUERY, {"date": target_date, "merchant_id": merchant_id}
                )
            }
        except Exception as exc:  # noqa: BLE001 -- re-raised, typed: never re-roll an unchecked day
            raise BilledDayCheckFailed(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
        rows = await database.fetch_all(
            _ROLLUP_QUERY,
            {"date": target_date, "merchant_id": merchant_id},
        )

        for row in rows:
            if str(_get(row, "merchant_id")) in invoiced:
                continue
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
            written += 1

    return {"written": written, "invoiced_merchants": sorted(invoiced)}


async def aggregate_daily(date: date) -> int:
    """Aggregate one day's attributed gross/refund/net GMV into daily rollups.

    The upstream gross edge amount is the v1.3 GMV basis:
    orders.subtotal - orders.discount_total. Tax, shipping, and orders.total are
    deliberately excluded from this service.
    """
    result = await _aggregate_for_date(date)
    if result["invoiced_merchants"]:
        logger.warning("gmv_rollup_day_left_as_billed date=%s merchants=%s",
                       date.isoformat(), result["invoiced_merchants"])
    return result["written"]


async def recompute_for_date(date: date, merchant_id: str) -> str:
    """Recompute one merchant's rollups for one day, unless an invoice already covers it.

    Returns RECOMPUTED or INVOICED_PERIOD_MANUAL_CREDIT. Raises BilledDayCheckFailed when the
    invoice check fails, and asyncpg's LockNotAvailableError when an invoice run holds the day
    longer than the lock timeout; nothing is written in either case.
    """
    result = await _aggregate_for_date(date, merchant_id=merchant_id, lock_timeout=True)
    if merchant_id in result["invoiced_merchants"]:
        return INVOICED_PERIOD_MANUAL_CREDIT
    return RECOMPUTED


async def lock_billing_days(period_start: date, period_end: date, *, db: Any) -> None:
    """Take the SHARED day lock on every day of a billing period, in ascending order.

    Call inside the transaction that reads the period's rollup rows and writes the invoice. A
    re-roll holds the same key exclusively, so it either finishes before the invoice run reads, or
    waits for the invoice to commit and then leaves the day as billed. Ascending order everywhere
    keeps two lockers from deadlocking. `db` is the caller's database handle: the locks must be
    taken on the connection of the caller's open transaction.
    """
    day = period_start
    while day <= period_end:
        await db.execute(LOCK_DAY_SHARED_QUERY, {"date": day})
        day += timedelta(days=1)


#: recompute_days_for_edges outcomes, per (merchant_id, day).
RECOMPUTED = "recomputed"
INVOICED_PERIOD_MANUAL_CREDIT = "invoiced_period_manual_credit"
INVOICE_CHECK_FAILED = "invoice_check_failed"
RECOMPUTE_FAILED = "recompute_failed"


async def _recompute_day_unless_invoiced(day: date, merchant_id: str, edge_ids: list[str]) -> str:
    # The invoice check runs inside recompute_for_date's day lock (see _aggregate_for_date), never
    # before it: a check made outside the lock is exactly the read-then-rewrite race.
    try:
        outcome = await recompute_for_date(day, merchant_id)
    except BilledDayCheckFailed as exc:
        logger.warning(
            "gmv_rollup_recompute_invoice_check_failed merchant_id=%s date=%s edge_ids=%s error=%s",
            merchant_id, day.isoformat(), edge_ids, str(exc)[:200],
        )
        return INVOICE_CHECK_FAILED
    except Exception as exc:  # noqa: BLE001 -- best-effort; the refund already stands
        logger.warning(
            "gmv_rollup_recompute_failed merchant_id=%s date=%s edge_ids=%s error_type=%s error=%s",
            merchant_id, day.isoformat(), edge_ids, type(exc).__name__, str(exc)[:200],
        )
        return RECOMPUTE_FAILED
    if outcome == INVOICED_PERIOD_MANUAL_CREDIT:
        # The edges name what to credit: each carries its refund_ids and refund_amount_cents.
        logger.warning(
            "gmv_rollup_recompute_skipped_invoiced_day merchant_id=%s date=%s edge_ids=%s: "
            "rollup left as billed, manual credit required",
            merchant_id, day.isoformat(), edge_ids,
        )
    return outcome


async def recompute_days_for_edges(edges: Iterable[Mapping[str, Any]]) -> dict[tuple[str, date], str]:
    """Best-effort: re-roll each (merchant, UTC creation day) the given refunded edges bill on.

    A refund changes an edge's refund_amount_cents, but the edge bills on the day it was
    created, and the daily job only rolls up yesterday. Every refund writer calls this after
    its write so a late refund reaches gmv_attribution_daily before that month is invoiced.

    A day an invoice already covers for the merchant is left as billed, checked under the day lock
    the invoice run also holds while it bills: invoice items point at its rollup rows
    and partner settlement reads them, so rewriting it would make the rollup disagree with the
    invoice the merchant received. There is no credit-note path; the refund stays on the edge
    and the day is logged for a manual credit. If the invoice check fails, the day is not
    re-rolled either.

    Call it AFTER the refund has committed, never inside that transaction: a failed statement
    here would leave the surrounding transaction aborted and take the refund with it. It never
    raises. Returns the outcome per (merchant_id, day); an edge with no merchant or creation
    time is logged and left out.
    """
    days: dict[tuple[str, date], list[str]] = {}
    for edge in edges:
        merchant_id = _get(edge, "merchant_id")
        try:
            if not merchant_id:
                raise ValueError("edge has no merchant_id")
            key = (str(merchant_id), _coerce_date(_get(edge, "created_at")))
            days.setdefault(key, []).append(str(_get(edge, "edge_id")))
        except Exception as exc:  # noqa: BLE001 -- best-effort; the refund already stands
            logger.warning(
                "gmv_rollup_recompute_skipped edge_id=%s merchant_id=%s error=%s",
                _get(edge, "edge_id"), merchant_id, str(exc)[:200],
            )
    return {
        (merchant_id, day): await _recompute_day_unless_invoiced(day, merchant_id, days[(merchant_id, day)])
        for merchant_id, day in sorted(days)
    }


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
