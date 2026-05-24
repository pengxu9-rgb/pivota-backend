from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Optional

from core.billing_constants import (
    PERSONAL_AGENT_NET_MARGIN,
    THIRD_PARTY_AGENT_NET_MARGIN,
)
from db.database import database


_BP_DENOMINATOR = Decimal("10000")
_ZERO_CENTS = Decimal("0")


@dataclass(frozen=True)
class _PartnerContract:
    channel_partner_id: int
    active_rate_scope: str
    gmv_take_definition: str
    per_brand_tail_months: int
    churn_clawback_days: int
    nonpayment_clawback_days: int


@dataclass(frozen=True)
class _ResolvedRate:
    stream: str
    brand_year: int
    rate_bp: int
    schedule_row_id: Optional[int]

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "brand_year": self.brand_year,
            "rate_bp": self.rate_bp,
            "schedule_row_id": self.schedule_row_id,
        }


async def compute_partner_comp_v2(
    channel_partner_id: int,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """Compute partner compensation from structured contract columns and statements.

    The returned top-level shape intentionally matches compute_partner_comp()
    so partner_settlement_service can write snapshots, balances, and payouts
    without a downstream schema fork.
    """

    calendar_month = _month_start(period_start)
    partner_contract = await _read_partner_contract(channel_partner_id)
    attributed_brand_rows = await _read_attributed_brands(channel_partner_id)

    total_subscription_share_cents = 0
    total_credit_overage_share_cents = 0
    total_gmv_share_cents = 0
    merchant_accruals: dict[str, dict[str, Any]] = {}
    brand_count_computed = 0
    brand_count_skipped_no_activation = 0
    brand_count_skipped_tail_exhausted = 0
    brand_count_suspended_nonpayment = 0

    for brand_row in attributed_brand_rows:
        merchant_id = str(_row_get(brand_row, "merchant_id"))
        activated_at = _row_get(brand_row, "activated_at")

        if activated_at is None:
            brand_count_skipped_no_activation += 1
            merchant_accruals[merchant_id] = _zero_merchant_accrual(
                merchant_id=merchant_id,
                brand_year=0,
                tail_exhausted=False,
                gmv_take_definition=partner_contract.gmv_take_definition,
                nonpayment_suspended=False,
            )
            continue

        brand_year, tail_exhausted = _resolve_brand_year(
            activated_at=activated_at,
            calendar_month=calendar_month,
            tail_months=partner_contract.per_brand_tail_months,
        )
        if tail_exhausted:
            brand_count_skipped_tail_exhausted += 1
            merchant_accruals[merchant_id] = _zero_merchant_accrual(
                merchant_id=merchant_id,
                brand_year=brand_year,
                tail_exhausted=True,
                gmv_take_definition=partner_contract.gmv_take_definition,
                nonpayment_suspended=False,
            )
            continue

        if await _merchant_has_nonpayment_suspension(
            merchant_id=merchant_id,
            nonpayment_clawback_days=partner_contract.nonpayment_clawback_days,
        ):
            brand_count_suspended_nonpayment += 1
            merchant_accruals[merchant_id] = _zero_merchant_accrual(
                merchant_id=merchant_id,
                brand_year=brand_year,
                tail_exhausted=False,
                gmv_take_definition=partner_contract.gmv_take_definition,
                nonpayment_suspended=True,
            )
            continue

        statement_row = await _read_brand_statement(merchant_id, calendar_month)
        if statement_row is None:
            merchant_accruals[merchant_id] = _zero_merchant_accrual(
                merchant_id=merchant_id,
                brand_year=brand_year,
                tail_exhausted=False,
                gmv_take_definition=partner_contract.gmv_take_definition,
                nonpayment_suspended=False,
            )
            continue

        brand_accrual = await _compute_per_brand_shares(
            partner_contract=partner_contract,
            statement_row=statement_row,
            brand_year=brand_year,
            calendar_month=calendar_month,
        )
        merchant_accruals[merchant_id] = brand_accrual
        brand_count_computed += 1

        # SHARE + SHARE: aggregate partner subscription share by brand.
        total_subscription_share_cents += _as_int(
            brand_accrual["subscription_share_cents"]
        )
        # SHARE + SHARE: aggregate partner credit-overage share by brand.
        total_credit_overage_share_cents += _as_int(
            brand_accrual["credit_overage_share_cents"]
        )
        # SHARE + SHARE: aggregate partner GMV share by brand.
        total_gmv_share_cents += _as_int(brand_accrual["gmv_share_cents"])

    clawbacks = await _compute_churn_clawbacks(
        partner_contract=partner_contract,
        attributed_brand_rows=attributed_brand_rows,
    )
    subsidy_cap_applied_cents = 0
    clawback_share_total_cents = _sum_clawback_share_cents(clawbacks)
    # SHARE + SHARE + SHARE: total partner shares across streams before PR #7 subsidies.
    gross_partner_share_total_cents = (
        total_subscription_share_cents
        + total_credit_overage_share_cents
        + total_gmv_share_cents
    )
    # SHARE - SHARE - SHARE: net payable partner share, floored at zero.
    net_comp_cents = max(
        gross_partner_share_total_cents
        - subsidy_cap_applied_cents
        - clawback_share_total_cents,
        0,
    )

    return {
        "subscription_rev_cents": total_subscription_share_cents,
        "gmv_take_rev_cents": total_gmv_share_cents,
        "credit_overage_rev_cents": total_credit_overage_share_cents,
        "subsidy_cap_applied_cents": subsidy_cap_applied_cents,
        "subsidy_cap_remaining_cents": None,
        "clawbacks": clawbacks,
        "net_comp_cents": net_comp_cents,
        "merchant_accruals": merchant_accruals,
        "commission_config": {
            "source": "structured_contract_columns",
            "active_rate_scope": partner_contract.active_rate_scope,
            "gmv_take_definition": partner_contract.gmv_take_definition,
            "per_brand_tail_months": partner_contract.per_brand_tail_months,
            "churn_clawback_days": partner_contract.churn_clawback_days,
            "nonpayment_clawback_days": partner_contract.nonpayment_clawback_days,
        },
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "v2_metadata": {
            "channel_partner_id": channel_partner_id,
            "active_rate_scope": partner_contract.active_rate_scope,
            "gmv_take_definition": partner_contract.gmv_take_definition,
            "per_brand_tail_months": partner_contract.per_brand_tail_months,
            "churn_clawback_days": partner_contract.churn_clawback_days,
            "nonpayment_clawback_days": partner_contract.nonpayment_clawback_days,
            "engine_version": "v2.0",
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "brand_count_computed": brand_count_computed,
            "brand_count_skipped_no_activation": brand_count_skipped_no_activation,
            "brand_count_skipped_tail_exhausted": brand_count_skipped_tail_exhausted,
            "brand_count_suspended_nonpayment": brand_count_suspended_nonpayment,
        },
    }


async def _read_partner_contract(channel_partner_id: int) -> _PartnerContract:
    row = await database.fetch_one(
        """
        SELECT
          id,
          active_rate_scope,
          gmv_take_definition,
          per_brand_tail_months,
          churn_clawback_days,
          nonpayment_clawback_days
        FROM channel_partners
        WHERE id = :channel_partner_id
        """,
        {"channel_partner_id": channel_partner_id},
    )
    if not row:
        raise ValueError(f"Channel partner not found: {channel_partner_id}")

    return _PartnerContract(
        channel_partner_id=int(_row_get(row, "id")),
        active_rate_scope=str(_row_get(row, "active_rate_scope") or "B"),
        gmv_take_definition=str(_row_get(row, "gmv_take_definition") or "net"),
        per_brand_tail_months=max(
            _as_int(_row_get(row, "per_brand_tail_months")),
            0,
        ),
        churn_clawback_days=_positive_int_or_default(
            _row_get(row, "churn_clawback_days"),
            90,
        ),
        nonpayment_clawback_days=_positive_int_or_default(
            _row_get(row, "nonpayment_clawback_days"),
            60,
        ),
    )


async def _read_attributed_brands(channel_partner_id: int) -> list[Any]:
    rows = await database.fetch_all(
        """
        SELECT DISTINCT merchant_id, activated_at
        FROM partner_attribution
        WHERE channel_partner_id = :channel_partner_id
          AND status IN ('registered', 'signed', 'active')
        ORDER BY merchant_id
        """,
        {"channel_partner_id": channel_partner_id},
    )
    return list(rows or [])


async def _read_brand_statement(merchant_id: str, calendar_month: date) -> Any | None:
    return await database.fetch_one(
        """
        SELECT
          merchant_id,
          calendar_month,
          subscription_revenue_usd_cents,
          overage_revenue_usd_cents,
          pivota_gmv_take_usd_cents,
          gmv_usd_cents,
          gmv_personal_usd_cents,
          gmv_third_party_usd_cents
        FROM monthly_brand_statements
        WHERE merchant_id = :merchant_id
          AND calendar_month = :calendar_month
          AND status IN ('frozen', 'invoiced')
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "calendar_month": calendar_month},
    )


async def _merchant_has_nonpayment_suspension(
    *,
    merchant_id: str,
    nonpayment_clawback_days: int,
) -> bool:
    row = await database.fetch_one(
        """
        SELECT 1
        FROM invoices
        WHERE merchant_id = :merchant_id
          AND status IN (
            'draft',
            'finalizing',
            'finalized',
            'failed',
            'payment_failed',
            'uncollectible'
          )
          AND COALESCE(due_date, created_at) < (
            CURRENT_DATE - :nonpayment_days * INTERVAL '1 day'
          )
        LIMIT 1
        """,
        {
            "merchant_id": merchant_id,
            "nonpayment_days": max(nonpayment_clawback_days, 1),
        },
    )
    return row is not None


async def _compute_churn_clawbacks(
    *,
    partner_contract: _PartnerContract,
    attributed_brand_rows: list[Any],
) -> list[dict[str, Any]]:
    clawbacks: list[dict[str, Any]] = []
    for brand_row in attributed_brand_rows:
        merchant_id = str(_row_get(brand_row, "merchant_id"))
        activated_at = _row_get(brand_row, "activated_at")
        if activated_at is None:
            continue
        if not await _merchant_churned_within_clawback_window(
            merchant_id=merchant_id,
            activated_at=activated_at,
            churn_clawback_days=partner_contract.churn_clawback_days,
        ):
            continue

        historical_accrued_share_cents = (
            await _historical_accrued_share_cents_for_merchant(
                channel_partner_id=partner_contract.channel_partner_id,
                merchant_id=merchant_id,
            )
        )
        if historical_accrued_share_cents <= 0:
            continue
        clawbacks.append(
            {
                "merchant_id": merchant_id,
                "amount_cents": historical_accrued_share_cents,
                "reason": "90_day_churn",
            }
        )
    return clawbacks


async def _merchant_churned_within_clawback_window(
    *,
    merchant_id: str,
    activated_at: date | datetime,
    churn_clawback_days: int,
) -> bool:
    row = await database.fetch_one(
        """
        SELECT us.id, us.status, us.canceled_at
        FROM user_subscriptions us
        WHERE us.merchant_id = :merchant_id
          AND NOT EXISTS (
            SELECT 1
            FROM user_subscriptions active_us
            WHERE active_us.merchant_id = us.merchant_id
              AND active_us.status IN ('active', 'trialing', 'past_due')
          )
        ORDER BY COALESCE(us.current_period_start, us.started_at, us.created_at) DESC, us.id DESC
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if not row:
        return False

    if str(_row_get(row, "status") or "") != "canceled":
        return False

    canceled_at = _row_get(row, "canceled_at")
    if canceled_at is None:
        return False

    activation_date = _as_date(activated_at)
    canceled_date = _as_date(canceled_at)
    days_from_activation = (canceled_date - activation_date).days
    return 0 <= days_from_activation <= max(churn_clawback_days, 1)


async def _historical_accrued_share_cents_for_merchant(
    *,
    channel_partner_id: int,
    merchant_id: str,
) -> int:
    rows = await database.fetch_all(
        """
        SELECT snapshot_payload_jsonb
        FROM settlement_snapshots
        WHERE channel_partner_id = :channel_partner_id
        ORDER BY created_at ASC
        """,
        {"channel_partner_id": channel_partner_id},
    )

    accrued_share_decimal_cents = Decimal("0")
    for row in rows or []:
        payload = _coerce_json(_row_get(row, "snapshot_payload_jsonb"))
        merchant_accruals = _coerce_json(payload.get("merchant_accruals"))
        merchant_payload = merchant_accruals.get(merchant_id)
        if not isinstance(merchant_payload, dict):
            continue
        # SHARE + SHARE: add prior credited partner share cents for this merchant.
        accrued_share_decimal_cents += Decimal(
            _as_int(merchant_payload.get("credited_comp_cents"))
        )
    return int(accrued_share_decimal_cents)


def _sum_clawback_share_cents(clawbacks: list[dict[str, Any]]) -> int:
    clawback_share_decimal_cents = Decimal("0")
    for clawback in clawbacks:
        # SHARE + SHARE: aggregate clawback share cents from reversal entries.
        clawback_share_decimal_cents += Decimal(_as_int(clawback.get("amount_cents")))
    return int(clawback_share_decimal_cents)


def _resolve_brand_year(
    *,
    activated_at: date | datetime,
    calendar_month: date,
    tail_months: int,
) -> tuple[int, bool]:
    """Map (activated_at, calendar_month) → (brand_year, tail_exhausted).

    Interpretation locked 2026-05-24 (codex post-merge review of PR #631–#641,
    HIGH #1): the architectural decision \"activation month counts as month 1 of
    tail\" governs. tail_months=36 means 36 payable months TOTAL, of which the
    first is the activation month itself.

    paid_month_index numbering:
      activation_month        → index 1 (Y1)
      activation_month + 1mo  → index 2 (Y1)
      ...
      activation_month + 11mo → index 12 (Y1, last Y1 month)
      activation_month + 12mo → index 13 (Y2 starts)
      activation_month + 23mo → index 24 (Y2 last)
      activation_month + 24mo → index 25 (Y3 starts)
      activation_month + 35mo → index 36 (Y3 last, last payable)
      activation_month + 36mo → index 37 → tail exhausted

    Diverges from build brief §9.5's table (which labels post-activation months
    1..36 and lists 2028-04 as payable). The architectural decision overrides
    the brief example; brief §9.5 should be updated in a follow-up doc PR.
    """

    activated_month = _month_start(_as_date(activated_at))
    months_since_activation = (
        (calendar_month.year - activated_month.year) * 12
        + (calendar_month.month - activated_month.month)
    )
    if months_since_activation < 0:
        return 0, False
    if months_since_activation >= tail_months:
        return 0, True

    paid_month_index = months_since_activation + 1
    brand_year = ((paid_month_index - 1) // 12) + 1
    if brand_year > 3:
        return 0, True
    return brand_year, False


async def _resolve_rate_bp(
    *,
    partner_id: int,
    scope: str,
    stream: str,
    brand_year: int,
    calendar_month: date,
) -> _ResolvedRate:
    row = await database.fetch_one(
        """
        SELECT rate_bp, id
        FROM partner_rate_schedules
        WHERE channel_partner_id = :partner_id
          AND scope = :scope
          AND stream = :stream
          AND brand_year = :brand_year
          AND effective_from <= :calendar_month
          AND (effective_to IS NULL OR effective_to > :calendar_month)
        ORDER BY effective_from DESC, id DESC
        LIMIT 1
        """,
        {
            "partner_id": partner_id,
            "scope": scope,
            "stream": stream,
            "brand_year": brand_year,
            "calendar_month": calendar_month,
        },
    )
    if not row:
        return _ResolvedRate(
            stream=stream,
            brand_year=brand_year,
            rate_bp=0,
            schedule_row_id=None,
        )
    return _ResolvedRate(
        stream=stream,
        brand_year=brand_year,
        rate_bp=_as_int(_row_get(row, "rate_bp")),
        schedule_row_id=_optional_int(_row_get(row, "id")),
    )


async def _compute_per_brand_shares(
    *,
    partner_contract: _PartnerContract,
    statement_row: Any,
    brand_year: int,
    calendar_month: date,
) -> dict[str, Any]:
    merchant_id = str(_row_get(statement_row, "merchant_id"))
    subscription_rate = await _resolve_rate_bp(
        partner_id=partner_contract.channel_partner_id,
        scope=partner_contract.active_rate_scope,
        stream="subscription",
        brand_year=brand_year,
        calendar_month=calendar_month,
    )
    credit_overage_rate = await _resolve_rate_bp(
        partner_id=partner_contract.channel_partner_id,
        scope=partner_contract.active_rate_scope,
        stream="credit_overage",
        brand_year=brand_year,
        calendar_month=calendar_month,
    )

    subscription_rev_raw_cents = _as_int(
        _row_get(statement_row, "subscription_revenue_usd_cents")
    )
    credit_overage_rev_raw_cents = _as_int(
        _row_get(statement_row, "overage_revenue_usd_cents")
    )
    pivota_gmv_take_raw_cents = _as_int(
        _row_get(statement_row, "pivota_gmv_take_usd_cents")
    )
    gmv_total_raw_cents = _as_int(_row_get(statement_row, "gmv_usd_cents"))
    gmv_personal_raw_cents = _as_int(
        _row_get(statement_row, "gmv_personal_usd_cents")
    )
    gmv_third_party_raw_cents = _as_int(
        _row_get(statement_row, "gmv_third_party_usd_cents")
    )

    # RAW * RATE: partner subscription share from raw subscription revenue.
    subscription_share_cents = _apply_rate_share_cents(
        subscription_rev_raw_cents,
        subscription_rate.rate_bp,
    )
    # RAW * RATE: partner credit-overage share from raw overage revenue.
    credit_overage_share_cents = _apply_rate_share_cents(
        credit_overage_rev_raw_cents,
        credit_overage_rate.rate_bp,
    )

    resolved_rates = [
        subscription_rate.as_snapshot(),
        credit_overage_rate.as_snapshot(),
    ]
    if partner_contract.gmv_take_definition == "gross":
        gmv_share_cents, gmv_rate_snapshots = await _compute_gmv_share_gross(
            partner_contract=partner_contract,
            brand_year=brand_year,
            calendar_month=calendar_month,
            pivota_gmv_take_raw_cents=pivota_gmv_take_raw_cents,
        )
    elif partner_contract.gmv_take_definition == "channel_tiered":
        gmv_share_cents, gmv_rate_snapshots = await _compute_gmv_share_channel_tiered(
            partner_contract=partner_contract,
            brand_year=brand_year,
            calendar_month=calendar_month,
            pivota_gmv_take_raw_cents=pivota_gmv_take_raw_cents,
            gmv_total_raw_cents=gmv_total_raw_cents,
            gmv_personal_raw_cents=gmv_personal_raw_cents,
            gmv_third_party_raw_cents=gmv_third_party_raw_cents,
        )
    else:
        gmv_share_cents, gmv_rate_snapshots = await _compute_gmv_share_net(
            partner_contract=partner_contract,
            brand_year=brand_year,
            calendar_month=calendar_month,
            pivota_gmv_take_raw_cents=pivota_gmv_take_raw_cents,
            gmv_total_raw_cents=gmv_total_raw_cents,
            gmv_personal_raw_cents=gmv_personal_raw_cents,
            gmv_third_party_raw_cents=gmv_third_party_raw_cents,
        )
    resolved_rates.extend(gmv_rate_snapshots)

    # SHARE + SHARE + SHARE: gross partner comp from per-stream shares only.
    gross_share_comp_cents = (
        subscription_share_cents
        + credit_overage_share_cents
        + gmv_share_cents
    )
    credited_share_comp_cents = gross_share_comp_cents

    return {
        "merchant_id": merchant_id,
        "brand_year": brand_year,
        "tail_exhausted": False,
        "subscription_rev_raw_cents": subscription_rev_raw_cents,
        "subscription_share_cents": subscription_share_cents,
        "credit_overage_rev_raw_cents": credit_overage_rev_raw_cents,
        "credit_overage_share_cents": credit_overage_share_cents,
        "pivota_gmv_take_raw_cents": pivota_gmv_take_raw_cents,
        "gmv_personal_raw_cents": gmv_personal_raw_cents,
        "gmv_third_party_raw_cents": gmv_third_party_raw_cents,
        "gmv_share_cents": gmv_share_cents,
        "gmv_share_definition_applied": partner_contract.gmv_take_definition,
        "resolved_rates": resolved_rates,
        "gross_comp_cents": gross_share_comp_cents,
        "credited_comp_cents": credited_share_comp_cents,
        "subsidy_cap_applied_cents": 0,
        "subsidy_cap_remaining_before_cents": None,
        "subsidy_cap_remaining_after_cents": None,
        "nonpayment_suspended": False,
    }


async def _compute_gmv_share_gross(
    *,
    partner_contract: _PartnerContract,
    brand_year: int,
    calendar_month: date,
    pivota_gmv_take_raw_cents: int,
) -> tuple[int, list[dict[str, Any]]]:
    gmv_rate = await _resolve_rate_bp(
        partner_id=partner_contract.channel_partner_id,
        scope=partner_contract.active_rate_scope,
        stream="gmv_take",
        brand_year=brand_year,
        calendar_month=calendar_month,
    )
    # TAKE * RATE: partner GMV share from Pivota's raw gross GMV take.
    gmv_share_cents = _apply_rate_share_cents(
        pivota_gmv_take_raw_cents,
        gmv_rate.rate_bp,
    )
    return gmv_share_cents, [gmv_rate.as_snapshot()]


async def _compute_gmv_share_net(
    *,
    partner_contract: _PartnerContract,
    brand_year: int,
    calendar_month: date,
    pivota_gmv_take_raw_cents: int,
    gmv_total_raw_cents: int,
    gmv_personal_raw_cents: int,
    gmv_third_party_raw_cents: int,
) -> tuple[int, list[dict[str, Any]]]:
    gmv_rate = await _resolve_rate_bp(
        partner_id=partner_contract.channel_partner_id,
        scope=partner_contract.active_rate_scope,
        stream="gmv_take",
        brand_year=brand_year,
        calendar_month=calendar_month,
    )
    personal_take_cents, third_party_take_cents = _split_pivota_take_by_gmv_ratio(
        pivota_gmv_take_raw_cents=pivota_gmv_take_raw_cents,
        gmv_total_raw_cents=gmv_total_raw_cents,
        gmv_personal_raw_cents=gmv_personal_raw_cents,
        gmv_third_party_raw_cents=gmv_third_party_raw_cents,
    )
    # TAKE * NET-MARGIN: Pivota personal-channel take after platform margin.
    personal_net_take_cents = _multiply_decimal_cents(
        personal_take_cents,
        Decimal(str(PERSONAL_AGENT_NET_MARGIN)),
    )
    # TAKE * NET-MARGIN: Pivota third-party-channel take after platform margin.
    third_party_net_take_cents = _multiply_decimal_cents(
        third_party_take_cents,
        Decimal(str(THIRD_PARTY_AGENT_NET_MARGIN)),
    )
    # TAKE + TAKE: Pivota net GMV take across channel slices before partner rate.
    pivota_gmv_net_take_cents = personal_net_take_cents + third_party_net_take_cents
    # TAKE * RATE: partner GMV share from Pivota's net GMV take.
    gmv_share_cents = _apply_rate_share_cents(
        pivota_gmv_net_take_cents,
        gmv_rate.rate_bp,
    )
    return gmv_share_cents, [gmv_rate.as_snapshot()]


async def _compute_gmv_share_channel_tiered(
    *,
    partner_contract: _PartnerContract,
    brand_year: int,
    calendar_month: date,
    pivota_gmv_take_raw_cents: int,
    gmv_total_raw_cents: int,
    gmv_personal_raw_cents: int,
    gmv_third_party_raw_cents: int,
) -> tuple[int, list[dict[str, Any]]]:
    personal_rate = await _resolve_rate_bp(
        partner_id=partner_contract.channel_partner_id,
        scope=partner_contract.active_rate_scope,
        stream="gmv_take_personal",
        brand_year=brand_year,
        calendar_month=calendar_month,
    )
    third_party_rate = await _resolve_rate_bp(
        partner_id=partner_contract.channel_partner_id,
        scope=partner_contract.active_rate_scope,
        stream="gmv_take_third_party",
        brand_year=brand_year,
        calendar_month=calendar_month,
    )
    personal_take_cents, third_party_take_cents = _split_pivota_take_by_gmv_ratio(
        pivota_gmv_take_raw_cents=pivota_gmv_take_raw_cents,
        gmv_total_raw_cents=gmv_total_raw_cents,
        gmv_personal_raw_cents=gmv_personal_raw_cents,
        gmv_third_party_raw_cents=gmv_third_party_raw_cents,
    )
    # TAKE * RATE: partner personal-channel GMV share from Pivota take.
    personal_share_cents = _apply_rate_share_cents(
        personal_take_cents,
        personal_rate.rate_bp,
    )
    # TAKE * RATE: partner third-party-channel GMV share from Pivota take.
    third_party_share_cents = _apply_rate_share_cents(
        third_party_take_cents,
        third_party_rate.rate_bp,
    )
    # SHARE + SHARE: partner GMV share across channel-specific rate rows.
    gmv_share_cents = personal_share_cents + third_party_share_cents
    return gmv_share_cents, [
        personal_rate.as_snapshot(),
        third_party_rate.as_snapshot(),
    ]


def _split_pivota_take_by_gmv_ratio(
    *,
    pivota_gmv_take_raw_cents: int,
    gmv_total_raw_cents: int,
    gmv_personal_raw_cents: int,
    gmv_third_party_raw_cents: int,
) -> tuple[int, int]:
    if gmv_total_raw_cents <= 0:
        return 0, 0

    personal_ratio = Decimal(gmv_personal_raw_cents) / Decimal(gmv_total_raw_cents)
    third_party_ratio = Decimal(gmv_third_party_raw_cents) / Decimal(
        gmv_total_raw_cents
    )
    # TAKE * RAW-RATIO: split Pivota raw gross GMV take by personal GMV share.
    personal_take_cents = _multiply_decimal_cents(
        pivota_gmv_take_raw_cents,
        personal_ratio,
    )
    # TAKE * RAW-RATIO: split Pivota raw gross GMV take by third-party GMV share.
    third_party_take_cents = _multiply_decimal_cents(
        pivota_gmv_take_raw_cents,
        third_party_ratio,
    )
    return personal_take_cents, third_party_take_cents


def _zero_merchant_accrual(
    *,
    merchant_id: str,
    brand_year: int,
    tail_exhausted: bool,
    gmv_take_definition: str,
    nonpayment_suspended: bool,
) -> dict[str, Any]:
    return {
        "merchant_id": merchant_id,
        "brand_year": brand_year,
        "tail_exhausted": tail_exhausted,
        "subscription_rev_raw_cents": 0,
        "subscription_share_cents": 0,
        "credit_overage_rev_raw_cents": 0,
        "credit_overage_share_cents": 0,
        "pivota_gmv_take_raw_cents": 0,
        "gmv_personal_raw_cents": 0,
        "gmv_third_party_raw_cents": 0,
        "gmv_share_cents": 0,
        "gmv_share_definition_applied": gmv_take_definition,
        "resolved_rates": [],
        "gross_comp_cents": 0,
        "credited_comp_cents": 0,
        "subsidy_cap_applied_cents": 0,
        "subsidy_cap_remaining_before_cents": None,
        "subsidy_cap_remaining_after_cents": None,
        "nonpayment_suspended": nonpayment_suspended,
    }


def _apply_rate_share_cents(amount_cents: int, rate_bp: int) -> int:
    bounded_amount_cents = max(amount_cents, 0)
    bounded_rate_bp = max(rate_bp, 0)
    # RAW/TAKE * RATE: convert source cents to partner share cents.
    share_decimal_cents = (
        Decimal(bounded_amount_cents) * Decimal(bounded_rate_bp) / _BP_DENOMINATOR
    )
    return _round_cents(share_decimal_cents)


def _multiply_decimal_cents(amount_cents: int, multiplier: Decimal) -> int:
    bounded_amount_cents = max(amount_cents, 0)
    if multiplier < _ZERO_CENTS:
        multiplier = _ZERO_CENTS
    # TAKE * DECIMAL: apply a ratio or net-margin multiplier to source cents.
    multiplied_decimal_cents = Decimal(bounded_amount_cents) * multiplier
    return _round_cents(multiplied_decimal_cents)


def _round_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _positive_int_or_default(value: Any, default: int) -> int:
    parsed = _as_int(value)
    if parsed <= 0:
        return default
    return parsed


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _coerce_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key)
