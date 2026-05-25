from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from core.billing_constants import (
    CREDIT_TO_USD_CENTS,
    DEFAULT_CREDIT_COST_RATIO,
    SAAS_GROSS_MARGIN_PCT,
    overage_revenue_cents,
)
from db.database import IS_POSTGRES, database


SYSTEM_DEFAULT_GMV_TAKE_RATE_BP = 1000


class StatementAlreadyFrozenError(Exception):
    """Raised when a statement transition requires an open row."""


class StatementInvalidTransitionError(Exception):
    """Raised when a statement cannot transition to the requested state."""


async def current_period_usage_snapshot(merchant_id: str) -> dict[str, Any]:
    """Merchant-safe current-month usage snapshot for billing self-serve UI."""

    if not merchant_id:
        raise ValueError("merchant_id is required")

    today = _utc_today()
    period_start = today.replace(day=1)
    period_end = _next_month(period_start)
    subscription = await _latest_active_subscription(
        merchant_id=merchant_id,
        period_start=period_start,
        period_end=period_end,
    )
    if not subscription:
        return {
            "tier": None,
            "allowance_credits": 0,
            "consumed_credits": 0,
            "overage_count": 0,
            "overage_total_usd_cents": 0,
            "days_remaining": 0,
            "period_start": period_start,
            "period_end": period_end,
            "in_overage": False,
        }

    credit_events = await _credit_consumption_events(
        merchant_id=merchant_id,
        period_start=period_start,
        period_end=period_end,
    )
    consumed_credits = -sum(
        int(_row_get(row, "credits_delta") or 0) for row in credit_events
    )
    allowance_credits = int(_row_get(subscription, "monthly_credit_allowance") or 0)
    overage_count = max(0, consumed_credits - allowance_credits)
    overage_total_raw_cents = overage_revenue_cents(overage_count)
    current_period_end = (
        _as_date_or_none(_row_get(subscription, "current_period_end")) or period_end
    )

    return {
        "tier": _row_get(subscription, "tier_name"),
        "allowance_credits": allowance_credits,
        "consumed_credits": consumed_credits,
        "overage_count": overage_count,
        "overage_total_usd_cents": overage_total_raw_cents,
        "days_remaining": max((current_period_end - today).days, 0),
        "period_start": period_start,
        "period_end": period_end,
        "in_overage": overage_count > 0,
    }


async def list_merchant_statement_rows(
    *,
    merchant_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Merchant-safe frozen/invoiced statement rows, newest first."""

    if not merchant_id:
        raise ValueError("merchant_id is required")
    safe_limit = min(max(int(limit), 1), 36)
    rows = await database.fetch_all(
        """
        SELECT
          calendar_month,
          tier_name,
          subscription_revenue_usd_cents,
          overage_credits,
          overage_revenue_usd_cents,
          status,
          frozen_at,
          invoiced_at
        FROM monthly_brand_statements
        WHERE merchant_id = :merchant_id
          AND status IN ('frozen', 'invoiced')
        ORDER BY calendar_month DESC
        LIMIT :limit
        """,
        {"merchant_id": merchant_id, "limit": safe_limit},
    )
    statements: list[dict[str, Any]] = []
    for row in rows or []:
        subscription_revenue_raw_cents = int(
            _row_get(row, "subscription_revenue_usd_cents") or 0
        )
        overage_revenue_raw_cents = int(
            _row_get(row, "overage_revenue_usd_cents") or 0
        )
        statements.append(
            {
                "calendar_month": _row_get(row, "calendar_month"),
                "tier_name": _row_get(row, "tier_name"),
                "subscription_revenue_usd_cents": subscription_revenue_raw_cents,
                "overage_credits": int(_row_get(row, "overage_credits") or 0),
                "overage_revenue_usd_cents": overage_revenue_raw_cents,
                "status": _row_get(row, "status"),
                "frozen_at": _row_get(row, "frozen_at"),
                "invoiced_at": _row_get(row, "invoiced_at"),
            }
        )
    return statements


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
    - GMV fields: raw classified net GMV from commerce_attribution_edges.
      Unclassified positive-GMV edges are excluded from gmv_usd_cents and counted in metadata.
    - total_revenue_usd_cents = sub_revenue + overage_revenue + pivota_gmv_take
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
        values = await _statement_values(
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
                  gmv_usd_cents = :gmv_usd_cents,
                  gmv_personal_usd_cents = :gmv_personal_usd_cents,
                  gmv_third_party_usd_cents = :gmv_third_party_usd_cents,
                  pivota_gmv_take_usd_cents = :pivota_gmv_take_usd_cents,
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
                  :gmv_usd_cents,
                  :gmv_personal_usd_cents,
                  :gmv_third_party_usd_cents,
                  :pivota_gmv_take_usd_cents,
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
          us.current_period_end AS current_period_end,
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


async def _statement_values(
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
    gmv_row = await _gmv_attribution_for_month(
        merchant_id=merchant_id,
        period_start=calendar_month,
        period_end=period_end,
    )
    # Raw classified brand-sales GMV in cents. These values are not partner
    # shares. Positive-GMV unclassified edges stay out of gmv_usd_cents so the
    # monthly_brand_statements GMV split CHECK remains true.
    gmv_personal_raw_cents = int(_row_get(gmv_row, "gmv_personal_cents") or 0)
    gmv_third_party_raw_cents = int(_row_get(gmv_row, "gmv_third_party_cents") or 0)
    gmv_total_raw_cents = gmv_personal_raw_cents + gmv_third_party_raw_cents
    gmv_unclassified_edge_count = int(_row_get(gmv_row, "unclassified_edge_count") or 0)
    # Pivota's raw GMV take in cents, before any partner-share calculation.
    # PR #5 replaces this system default with a partner_rate_schedules lookup.
    pivota_gmv_take_cents = int(
        round(gmv_total_raw_cents * SYSTEM_DEFAULT_GMV_TAKE_RATE_BP / 10000)
    )
    total_revenue = sub_revenue + overage_revenue + pivota_gmv_take_cents
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
        "gmv_personal_usd_cents": gmv_personal_raw_cents,
        "gmv_third_party_usd_cents": gmv_third_party_raw_cents,
        "gmv_unclassified_edge_count": gmv_unclassified_edge_count,
        "gmv_take_rate_bp_applied": SYSTEM_DEFAULT_GMV_TAKE_RATE_BP,
        "gmv_take_rate_source": "system_default_pr4_pending_pr5_partner_lookup",
        "gmv_take_cogs_pending_pr5": True,
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
        "gmv_usd_cents": gmv_total_raw_cents,
        "gmv_personal_usd_cents": gmv_personal_raw_cents,
        "gmv_third_party_usd_cents": gmv_third_party_raw_cents,
        "pivota_gmv_take_usd_cents": pivota_gmv_take_cents,
        "total_revenue_usd_cents": total_revenue,
        "total_cogs_usd_cents": total_cogs,
        "pivota_gross_margin_usd_cents": gross_margin,
        "metadata_json": json.dumps(metadata, sort_keys=True),
    }


async def _gmv_attribution_for_month(
    *,
    merchant_id: str,
    period_start: date,
    period_end: date,
) -> Any:
    return await database.fetch_one(
        """
        SELECT
          COALESCE(SUM(net_attributed_gmv_cents), 0) AS gmv_raw_total_cents,
          COALESCE(
            SUM(net_attributed_gmv_cents) FILTER (WHERE gmv_channel = 'personal_agent'),
            0
          ) AS gmv_personal_cents,
          COALESCE(
            SUM(net_attributed_gmv_cents) FILTER (WHERE gmv_channel = 'third_party_agent'),
            0
          ) AS gmv_third_party_cents,
          COUNT(*) FILTER (WHERE gmv_channel IS NULL) AS unclassified_edge_count
        FROM commerce_attribution_edges
        WHERE merchant_id = :merchant_id
          AND created_at >= :period_start
          AND created_at < :period_end
          AND net_attributed_gmv_cents > 0
        """,
        {
            "merchant_id": merchant_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    )


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


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _as_date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value[:10])
    return None


def _json_param(name: str) -> str:
    return f"CAST(:{name} AS jsonb)" if IS_POSTGRES else f":{name}"


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key)


def _row_text(row: Any, key: str) -> str:
    value = _row_get(row, key)
    return str(value or "").strip().lower()
