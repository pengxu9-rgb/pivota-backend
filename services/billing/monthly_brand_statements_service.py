from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from core.billing_constants import (
    CREDIT_TO_USD_CENTS,
    DEFAULT_CREDIT_COST_RATIO,
    SAAS_GROSS_MARGIN_PCT,
    overage_revenue_cents,
)
from db.database import IS_POSTGRES, database


class StatementAlreadyFrozenError(Exception):
    """Raised when a statement transition requires an open row."""


class StatementInvalidTransitionError(Exception):
    """Raised when a statement cannot transition to the requested state."""


async def assemble_for_month(merchant_id: str, calendar_month: date) -> int:
    """Idempotently assemble or return the existing statement row id for (merchant_id, calendar_month).

    Behavior:
    - If a row exists with status='frozen' or 'invoiced': return its id unchanged. Never overwrite.
    - If a row exists with status='open': re-run the aggregation, UPDATE the columns, leave status='open'.
    - If no row exists: INSERT.

    Aggregation inputs (read-only on these):
    - credit_ledger.credits_delta for the merchant in [calendar_month, calendar_month + 1 month):
        credits_consumed = sum of negative deltas, taken positive (a consumption is a NEGATIVE delta)
                         = -SUM(credits_delta) FILTER (WHERE credits_delta < 0)
    - user_subscriptions JOIN subscription_plans for the active plan during this month:
        subscription_plan_id = active plan id
        tier_name = subscription_plans.name
        allowance = subscription_plans.monthly_credit_allowance
        sub_revenue_usd_cents = subscription_plans.price_cents (per build brief §6.1)
    - bundled_credits_consumed = min(allowance, credits_consumed)
    - overage_credits = max(0, credits_consumed - allowance)
    - overage_revenue_usd_cents = overage_revenue_cents(overage_credits) from core.billing_constants
    - GMV fields: ZERO in this PR. (PR #4 populates them.)
    - total_revenue_usd_cents = sub_revenue + overage_revenue + pivota_gmv_take (gmv_take is 0 in this PR)
    - total_cogs_usd_cents: per brief §6.3, computed but INTERNAL.
        bundled_credit_cogs  = bundled_credits_consumed × CREDIT_TO_USD_CENTS × DEFAULT_CREDIT_COST_RATIO
        overage_credit_cogs  = overage_credits          × CREDIT_TO_USD_CENTS × DEFAULT_CREDIT_COST_RATIO
        saas_cogs            = sub_revenue × (1 - SAAS_GROSS_MARGIN_PCT)
        total_cogs           = bundled_credit_cogs + overage_credit_cogs + saas_cogs (rounded once)
    - pivota_gross_margin_usd_cents = total_revenue - total_cogs

    Edge cases:
    - Merchant with no subscription that month: subscription_plan_id=NULL, sub_revenue=0, tier_name=NULL, allowance=0, all credits → overage.
      (This is unusual but possible if a brand consumed credits before subscribing or after cancellation; flag in metadata.)
    - Multiple subscriptions in the month (plan upgrade mid-month): use the LATEST active plan. Document the tradeoff in code comments; PR #6 will refine.
    - Negative credit_ledger sums (rare grants happened mid-month): treat consumption as positive only; grants don't reduce consumption count.

    Idempotency: hash the (credit_ledger event ids, subscription plan id) into metadata['assembly_hash'] so re-runs on the same data produce identical rows.

    Returns: the statement row id (int).
    """

    if not merchant_id:
        raise ValueError("merchant_id is required")
    _validate_month_start(calendar_month)
    period_end = _next_month(calendar_month)

    async with database.transaction():
        existing = await database.fetch_one(
            """
            SELECT id, status
            FROM monthly_brand_statements
            WHERE merchant_id = :merchant_id
              AND calendar_month = :calendar_month
            FOR UPDATE
            """,
            {"merchant_id": merchant_id, "calendar_month": calendar_month},
        )
        if existing and _row_text(existing, "status") in {"frozen", "invoiced"}:
            return int(_row_get(existing, "id"))

        credit_events = await _credit_consumption_events(
            merchant_id=merchant_id,
            period_start=calendar_month,
            period_end=period_end,
        )
        credits_consumed = -sum(
            int(_row_get(row, "credits_delta") or 0) for row in credit_events
        )
        subscription = await _latest_active_subscription(
            merchant_id=merchant_id,
            period_start=calendar_month,
            period_end=period_end,
        )
        values = _statement_values(
            merchant_id=merchant_id,
            calendar_month=calendar_month,
            period_end=period_end,
            credit_events=credit_events,
            credits_consumed=credits_consumed,
            subscription=subscription,
        )

        if existing:
            row = await database.fetch_one(
                f"""
                UPDATE monthly_brand_statements
                SET
                  subscription_plan_id = :subscription_plan_id,
                  tier_name = :tier_name,
                  subscription_revenue_usd_cents = :subscription_revenue_usd_cents,
                  credits_consumed = :credits_consumed,
                  bundled_credits_consumed = :bundled_credits_consumed,
                  overage_credits = :overage_credits,
                  overage_revenue_usd_cents = :overage_revenue_usd_cents,
                  gmv_usd_cents = 0,
                  gmv_personal_usd_cents = 0,
                  gmv_third_party_usd_cents = 0,
                  pivota_gmv_take_usd_cents = 0,
                  total_revenue_usd_cents = :total_revenue_usd_cents,
                  total_cogs_usd_cents = :total_cogs_usd_cents,
                  pivota_gross_margin_usd_cents = :pivota_gross_margin_usd_cents,
                  metadata = {_json_param('metadata_json')},
                  updated_at = NOW()
                WHERE id = :statement_id
                  AND status = 'open'
                RETURNING id
                """,
                {**values, "statement_id": int(_row_get(existing, "id"))},
            )
        else:
            row = await database.fetch_one(
                f"""
                INSERT INTO monthly_brand_statements (
                  merchant_id,
                  calendar_month,
                  subscription_plan_id,
                  tier_name,
                  subscription_revenue_usd_cents,
                  credits_consumed,
                  bundled_credits_consumed,
                  overage_credits,
                  overage_revenue_usd_cents,
                  gmv_usd_cents,
                  gmv_personal_usd_cents,
                  gmv_third_party_usd_cents,
                  pivota_gmv_take_usd_cents,
                  total_revenue_usd_cents,
                  total_cogs_usd_cents,
                  pivota_gross_margin_usd_cents,
                  status,
                  metadata
                ) VALUES (
                  :merchant_id,
                  :calendar_month,
                  :subscription_plan_id,
                  :tier_name,
                  :subscription_revenue_usd_cents,
                  :credits_consumed,
                  :bundled_credits_consumed,
                  :overage_credits,
                  :overage_revenue_usd_cents,
                  0,
                  0,
                  0,
                  0,
                  :total_revenue_usd_cents,
                  :total_cogs_usd_cents,
                  :pivota_gross_margin_usd_cents,
                  'open',
                  {_json_param('metadata_json')}
                )
                RETURNING id
                """,
                values,
            )

        if not row:
            raise StatementInvalidTransitionError(
                f"Unable to assemble statement for {merchant_id} {calendar_month.isoformat()}"
            )
        return int(_row_get(row, "id"))


async def freeze(statement_id: int) -> None:
    """Transition status open → frozen. Sets frozen_at = NOW().
    Raises StatementAlreadyFrozenError if not open.
    Once frozen, the BEFORE UPDATE trigger blocks further field mutations except status/invoiced_at/overage_invoice_id."""

    row = await database.fetch_one(
        """
        UPDATE monthly_brand_statements
        SET status = 'frozen',
            frozen_at = COALESCE(frozen_at, NOW()),
            updated_at = NOW()
        WHERE id = :statement_id
          AND status = 'open'
        RETURNING id
        """,
        {"statement_id": statement_id},
    )
    if not row:
        raise StatementAlreadyFrozenError(
            f"Statement {statement_id} is not open and cannot be frozen"
        )


async def mark_invoiced(statement_id: int, overage_invoice_id: int | None) -> None:
    """Transition status frozen → invoiced. Sets invoiced_at, overage_invoice_id.
    Raises if status != 'frozen' or overage_invoice_id is invalid. A NULL invoice id is
    only valid for the no-overage finalization path."""

    if overage_invoice_id is not None:
        invoice = await database.fetch_one(
            "SELECT id FROM invoices WHERE id = :invoice_id LIMIT 1",
            {"invoice_id": overage_invoice_id},
        )
        if not invoice:
            raise ValueError(f"Invalid overage_invoice_id: {overage_invoice_id}")

    row = await database.fetch_one(
        """
        UPDATE monthly_brand_statements
        SET status = 'invoiced',
            invoiced_at = NOW(),
            overage_invoice_id = :overage_invoice_id,
            updated_at = NOW()
        WHERE id = :statement_id
          AND status = 'frozen'
        RETURNING id
        """,
        {
            "statement_id": statement_id,
            "overage_invoice_id": overage_invoice_id,
        },
    )
    if not row:
        raise StatementInvalidTransitionError(
            f"Statement {statement_id} must be frozen before it can be marked invoiced"
        )


async def _credit_consumption_events(
    *,
    merchant_id: str,
    period_start: date,
    period_end: date,
) -> list[Any]:
    rows = await database.fetch_all(
        """
        SELECT id, credits_delta
        FROM credit_ledger
        WHERE merchant_id = :merchant_id
          AND occurred_at >= :period_start
          AND occurred_at < :period_end
          AND credits_delta < 0
        ORDER BY id ASC
        """,
        {
            "merchant_id": merchant_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    )
    return list(rows or [])


async def _latest_active_subscription(
    *,
    merchant_id: str,
    period_start: date,
    period_end: date,
) -> Any | None:
    # If a brand has multiple active subscription rows in a month, prefer the
    # latest period/start timestamp. That approximates upgrades until PR #6
    # adds lifecycle-aware proration and activation semantics.
    return await database.fetch_one(
        """
        SELECT
          us.plan_id AS subscription_plan_id,
          sp.name AS tier_name,
          sp.monthly_credit_allowance AS monthly_credit_allowance,
          sp.price_cents AS subscription_revenue_usd_cents
        FROM user_subscriptions us
        JOIN subscription_plans sp ON sp.id = us.plan_id
        WHERE us.merchant_id = :merchant_id
          AND us.status IN ('active', 'trialing', 'past_due')
          AND (us.current_period_start IS NULL OR us.current_period_start < :period_end)
          AND (us.current_period_end IS NULL OR us.current_period_end > :period_start)
        ORDER BY
          us.current_period_start DESC NULLS LAST,
          us.started_at DESC NULLS LAST,
          us.updated_at DESC NULLS LAST,
          us.id DESC
        LIMIT 1
        """,
        {
            "merchant_id": merchant_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    )


def _statement_values(
    *,
    merchant_id: str,
    calendar_month: date,
    period_end: date,
    credit_events: list[Any],
    credits_consumed: int,
    subscription: Any | None,
) -> dict[str, Any]:
    if subscription:
        subscription_plan_id = int(_row_get(subscription, "subscription_plan_id"))
        tier_name = _row_get(subscription, "tier_name")
        allowance = int(_row_get(subscription, "monthly_credit_allowance") or 0)
        sub_revenue = int(_row_get(subscription, "subscription_revenue_usd_cents") or 0)
        no_subscription = False
    else:
        subscription_plan_id = None
        tier_name = None
        allowance = 0
        sub_revenue = 0
        no_subscription = True

    bundled_credits = min(allowance, credits_consumed)
    overage_credits = max(0, credits_consumed - allowance)
    overage_revenue = overage_revenue_cents(overage_credits)
    total_revenue = sub_revenue + overage_revenue
    total_cogs = _total_cogs_cents(
        bundled_credits_consumed=bundled_credits,
        overage_credits=overage_credits,
        subscription_revenue_usd_cents=sub_revenue,
    )
    gross_margin = total_revenue - total_cogs
    event_ids = [int(_row_get(row, "id")) for row in credit_events]
    metadata = {
        "assembly_hash": _assembly_hash(
            credit_ledger_event_ids=event_ids,
            subscription_plan_id=subscription_plan_id,
        ),
        "credit_ledger_event_count": len(event_ids),
        "credit_ledger_event_ids": event_ids,
        "computed_at": "now()",
        "period_start": calendar_month.isoformat(),
        "period_end": period_end.isoformat(),
        "subscription_plan_id": subscription_plan_id,
        "no_subscription_for_month": no_subscription,
        "gmv_populated_by_pr": 4,
    }

    return {
        "merchant_id": merchant_id,
        "calendar_month": calendar_month,
        "subscription_plan_id": subscription_plan_id,
        "tier_name": tier_name,
        "subscription_revenue_usd_cents": sub_revenue,
        "credits_consumed": credits_consumed,
        "bundled_credits_consumed": bundled_credits,
        "overage_credits": overage_credits,
        "overage_revenue_usd_cents": overage_revenue,
        "total_revenue_usd_cents": total_revenue,
        "total_cogs_usd_cents": total_cogs,
        "pivota_gross_margin_usd_cents": gross_margin,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


def _total_cogs_cents(
    *,
    bundled_credits_consumed: int,
    overage_credits: int,
    subscription_revenue_usd_cents: int,
) -> int:
    credit_cogs = (
        Decimal(bundled_credits_consumed + overage_credits)
        * Decimal(CREDIT_TO_USD_CENTS)
        * Decimal(str(DEFAULT_CREDIT_COST_RATIO))
    )
    saas_cogs = Decimal(subscription_revenue_usd_cents) * (
        Decimal("1") - Decimal(str(SAAS_GROSS_MARGIN_PCT))
    )
    return int(round(credit_cogs + saas_cogs))


def _assembly_hash(
    *,
    credit_ledger_event_ids: list[int],
    subscription_plan_id: int | None,
) -> str:
    payload = json.dumps(
        {
            "credit_ledger_event_ids": credit_ledger_event_ids,
            "subscription_plan_id": subscription_plan_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _validate_month_start(value: date) -> None:
    if value.day != 1:
        raise ValueError("calendar_month must be the first day of a calendar month")


def _json_param(name: str) -> str:
    return f"CAST(:{name} AS jsonb)" if IS_POSTGRES else f":{name}"


def _row_get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except Exception:
        return getattr(row, key)


def _row_text(row: Any, key: str) -> str:
    value = _row_get(row, key)
    return str(value or "").strip().lower()
