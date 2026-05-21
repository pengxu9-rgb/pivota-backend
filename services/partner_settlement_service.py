from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import stripe

from config.settings import settings
from db.database import IS_POSTGRES, database


logger = logging.getLogger(__name__)

stripe_client = stripe.StripeClient(api_key=settings.stripe_secret_key or "")

_TABLE_COLUMN_CACHE: dict[str, set[str]] = {}
_INTROSPECTABLE_TABLES = {
    "agent_payouts",
    "channel_partners",
    "merchants",
    "partner_balance_ledger",
    "settlement_snapshots",
}


class SettlementAlreadyExistsError(Exception):
    """Raised when a partner settlement snapshot already exists for a billing run."""


class PayoutNotPendingError(Exception):
    """Raised when an admin tries to approve a payout that is not pending."""


class PayoutMissingConnectAccountError(Exception):
    """Raised when a channel partner payout has no Stripe Connect destination."""


async def run_settlement(billing_run_id: int) -> int:
    """Run partner settlement for a completed billing run.

    The function creates immutable settlement snapshots, credits current-period
    compensation to partner balances, applies clawbacks as future-balance ledger
    debits, and creates pending payout rows when the resulting balance is positive.
    """

    billing_run = await _fetch_billing_run(billing_run_id)
    period_start = _to_date(_row_get(billing_run, "period_start"))
    period_end = _to_date(_row_get(billing_run, "period_end"))

    partner_rows = await database.fetch_all(
        """
        SELECT channel_partner_id
        FROM (
          SELECT DISTINCT pa.channel_partner_id
          FROM partner_attribution pa
          WHERE EXISTS (
            SELECT 1
            FROM commerce_attribution_edges cae
            WHERE cae.merchant_id = pa.merchant_id
              AND DATE(cae.created_at) BETWEEN :period_start AND :period_end
          )

          UNION

          SELECT DISTINCT gad.channel_partner_id
          FROM gmv_attribution_daily gad
          WHERE gad.channel_partner_id IS NOT NULL
            AND gad.date BETWEEN :period_start AND :period_end
        ) partner_scope
        WHERE channel_partner_id IS NOT NULL
        ORDER BY channel_partner_id
        """,
        {"period_start": period_start, "period_end": period_end},
    )

    payout_count = 0
    for partner_row in partner_rows:
        channel_partner_id = int(_row_get(partner_row, "channel_partner_id"))
        comp_dict = await compute_partner_comp(channel_partner_id, period_start, period_end)
        snapshot_id = await write_settlement_snapshot(
            billing_run_id,
            channel_partner_id,
            comp_dict,
        )

        net_comp_cents = _as_int(comp_dict.get("net_comp_cents"))
        if net_comp_cents > 0:
            await credit_partner_balance(channel_partner_id, net_comp_cents, snapshot_id)

        for clawback in comp_dict.get("clawbacks", []):
            clawback_metadata = dict(clawback)
            clawback_metadata["billing_run_id"] = billing_run_id
            clawback_metadata["source_snapshot_id"] = snapshot_id
            await debit_partner_balance(
                channel_partner_id,
                _as_int(clawback.get("amount_cents")),
                event_type="clawback",
                metadata=clawback_metadata,
            )

        payout_id = await create_payout(channel_partner_id, billing_run_id, snapshot_id)
        if payout_id is not None:
            payout_count += 1

    return payout_count


async def compute_partner_comp(
    channel_partner_id: int,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """Compute one channel partner's compensation for a billing period."""

    partner_row = await database.fetch_one(
        """
        SELECT id, commission_config_json
        FROM channel_partners
        WHERE id = :channel_partner_id
        """,
        {"channel_partner_id": channel_partner_id},
    )
    if not partner_row:
        raise ValueError(f"Channel partner not found: {channel_partner_id}")

    config = _coerce_json(_row_get(partner_row, "commission_config_json"))
    subscription_share_bp = _as_int(config.get("subscription_rev_share_bp"))
    gmv_take_share_bp = _as_int(config.get("gmv_take_share_bp"))
    subsidy_cap_cents = _as_int(config.get("subsidy_cap_cents"))

    subscription_revenue_by_merchant = await _subscription_revenue_by_merchant(
        channel_partner_id,
        period_start,
    )
    gmv_take_by_merchant = await _gmv_take_revenue_by_merchant(
        channel_partner_id,
        period_start,
        period_end,
    )
    attributed_merchants = await _attributed_merchants(channel_partner_id)

    merchant_ids = sorted(
        set(attributed_merchants)
        | set(subscription_revenue_by_merchant)
        | set(gmv_take_by_merchant)
    )

    subscription_rev_cents = 0
    gmv_take_rev_cents = 0
    subsidy_cap_applied_cents = 0
    subsidy_remaining_total: Optional[int] = 0 if subsidy_cap_cents > 0 else None
    merchant_accruals: dict[str, dict[str, Any]] = {}

    for merchant_id in merchant_ids:
        merchant_subscription_comp = _apply_bp_share(
            subscription_revenue_by_merchant.get(merchant_id, 0),
            subscription_share_bp,
        )
        merchant_gmv_comp = _apply_bp_share(
            gmv_take_by_merchant.get(merchant_id, 0),
            gmv_take_share_bp,
        )
        merchant_comp = merchant_subscription_comp + merchant_gmv_comp

        cap_remaining_before: Optional[int] = None
        cap_remaining_after: Optional[int] = None
        merchant_subsidy_applied = 0
        credited_comp = merchant_comp

        if subsidy_cap_cents > 0:
            cap_remaining_before = await _current_subsidy_cap_remaining(
                channel_partner_id,
                merchant_id,
                default_cap_cents=subsidy_cap_cents,
            )
            credited_comp = min(merchant_comp, cap_remaining_before)
            merchant_subsidy_applied = max(merchant_comp - credited_comp, 0)
            cap_remaining_after = max(cap_remaining_before - credited_comp, 0)
            subsidy_cap_applied_cents += merchant_subsidy_applied
            subsidy_remaining_total = (subsidy_remaining_total or 0) + cap_remaining_after

        subscription_rev_cents += merchant_subscription_comp
        gmv_take_rev_cents += merchant_gmv_comp
        merchant_accruals[merchant_id] = {
            "merchant_id": merchant_id,
            "subscription_rev_cents": merchant_subscription_comp,
            "gmv_take_rev_cents": merchant_gmv_comp,
            "gross_comp_cents": merchant_comp,
            "credited_comp_cents": credited_comp,
            "subsidy_cap_applied_cents": merchant_subsidy_applied,
            "subsidy_cap_remaining_before_cents": cap_remaining_before,
            "subsidy_cap_remaining_after_cents": cap_remaining_after,
        }

    clawbacks = await _compute_churn_clawbacks(channel_partner_id, merchant_ids)
    clawback_total = sum(_as_int(item.get("amount_cents")) for item in clawbacks)
    credit_overage_rev_cents = 0
    net_comp_cents = max(
        subscription_rev_cents
        + gmv_take_rev_cents
        + credit_overage_rev_cents
        - subsidy_cap_applied_cents
        - clawback_total,
        0,
    )

    return {
        "subscription_rev_cents": subscription_rev_cents,
        "gmv_take_rev_cents": gmv_take_rev_cents,
        "credit_overage_rev_cents": credit_overage_rev_cents,
        "subsidy_cap_applied_cents": subsidy_cap_applied_cents,
        "subsidy_cap_remaining_cents": subsidy_remaining_total,
        "clawbacks": clawbacks,
        "net_comp_cents": net_comp_cents,
        "merchant_accruals": merchant_accruals,
        "commission_config": config,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }


async def write_settlement_snapshot(
    billing_run_id: int,
    channel_partner_id: int,
    snapshot_payload: dict[str, Any],
) -> int:
    """Insert an immutable settlement snapshot and return its id."""

    existing = await database.fetch_one(
        """
        SELECT id
        FROM settlement_snapshots
        WHERE billing_run_id = :billing_run_id
          AND channel_partner_id = :channel_partner_id
        LIMIT 1
        """,
        {
            "billing_run_id": billing_run_id,
            "channel_partner_id": channel_partner_id,
        },
    )
    if existing:
        raise SettlementAlreadyExistsError(
            "Settlement snapshot already exists for "
            f"billing_run_id={billing_run_id} channel_partner_id={channel_partner_id}"
        )

    payload_json = json.dumps(snapshot_payload, default=str)
    payload_value_sql = _json_value_sql("snapshot_payload_json")
    row = await database.fetch_one(
        f"""
        INSERT INTO settlement_snapshots (
          billing_run_id,
          channel_partner_id,
          snapshot_payload_jsonb,
          computed_comp_cents,
          subsidy_cap_remaining_at_snapshot,
          created_at
        )
        VALUES (
          :billing_run_id,
          :channel_partner_id,
          {payload_value_sql},
          :computed_comp_cents,
          :subsidy_cap_remaining_cents,
          {_now_sql()}
        )
        RETURNING id
        """,
        {
            "billing_run_id": billing_run_id,
            "channel_partner_id": channel_partner_id,
            "snapshot_payload_json": payload_json,
            "computed_comp_cents": _as_int(snapshot_payload.get("net_comp_cents")),
            "subsidy_cap_remaining_cents": _optional_int(
                snapshot_payload.get("subsidy_cap_remaining_cents")
            ),
        },
    )
    if not row:
        raise RuntimeError("Settlement snapshot insert did not return an id")
    return int(_row_get(row, "id"))


async def credit_partner_balance(
    channel_partner_id: int,
    amount_cents: int,
    snapshot_id: int,
) -> None:
    """Credit a partner balance and append a settlement ledger event."""

    if amount_cents <= 0:
        return

    async with database.transaction():
        await _ensure_partner_balance_row(channel_partner_id)
        balance_row = await database.fetch_one(
            """
            UPDATE partner_balance
            SET balance_cents = balance_cents + :amount_cents
            WHERE channel_partner_id = :channel_partner_id
            RETURNING balance_cents
            """,
            {
                "channel_partner_id": channel_partner_id,
                "amount_cents": amount_cents,
            },
        )
        if not balance_row:
            raise RuntimeError(f"Unable to credit partner balance: {channel_partner_id}")

        balance_after = _as_int(_row_get(balance_row, "balance_cents"))
        await _insert_partner_balance_ledger(
            channel_partner_id=channel_partner_id,
            event_type="settlement_added",
            signed_amount_cents=amount_cents,
            balance_after=balance_after,
            source_snapshot_id=snapshot_id,
            metadata={},
        )


async def debit_partner_balance(
    channel_partner_id: int,
    amount_cents: int,
    event_type: str,
    metadata: dict[str, Any],
) -> None:
    """Debit a partner balance and append a ledger event.

    This is the clawback path for churn and the payout path for successful Stripe
    transfers. Balance is allowed to go negative and is never floored here.
    """

    if event_type not in {"clawback", "payout"}:
        raise ValueError("event_type must be 'clawback' or 'payout'")
    if amount_cents <= 0:
        return

    async with database.transaction():
        await _ensure_partner_balance_row(channel_partner_id)
        balance_row = await database.fetch_one(
            """
            UPDATE partner_balance
            SET balance_cents = balance_cents - :amount_cents
            WHERE channel_partner_id = :channel_partner_id
            RETURNING balance_cents
            """,
            {
                "channel_partner_id": channel_partner_id,
                "amount_cents": amount_cents,
            },
        )
        if not balance_row:
            raise RuntimeError(f"Unable to debit partner balance: {channel_partner_id}")

        balance_after = _as_int(_row_get(balance_row, "balance_cents"))
        await _insert_partner_balance_ledger(
            channel_partner_id=channel_partner_id,
            event_type=event_type,
            signed_amount_cents=-amount_cents,
            balance_after=balance_after,
            source_snapshot_id=_optional_int(metadata.get("source_snapshot_id")),
            metadata=metadata,
        )


async def create_payout(
    channel_partner_id: int,
    billing_run_id: int,
    snapshot_id: int,
) -> int | None:
    """Create a pending channel partner payout when the balance is positive."""

    balance_row = await database.fetch_one(
        """
        SELECT balance_cents
        FROM partner_balance
        WHERE channel_partner_id = :channel_partner_id
        """,
        {"channel_partner_id": channel_partner_id},
    )
    balance_cents = _as_int(_row_get(balance_row, "balance_cents")) if balance_row else 0
    if balance_cents <= 0:
        return None

    billing_run = await _fetch_billing_run(billing_run_id)
    snapshot_payload = await _fetch_snapshot_payload(snapshot_id)
    agent_payout_columns = await _table_columns("agent_payouts")

    insert_columns = []
    insert_values_sql = []
    values: dict[str, Any] = {
        "channel_partner_id": channel_partner_id,
        "billing_run_id": billing_run_id,
        "snapshot_id": snapshot_id,
        "amount": Decimal(balance_cents) / Decimal("100"),
        "period_start": _row_get(billing_run, "period_start"),
        "period_end": _row_get(billing_run, "period_end"),
        "legacy_payee": f"channel_partner:{channel_partner_id}",
        "subsidy_cap_remaining_cents": _optional_int(
            snapshot_payload.get("subsidy_cap_remaining_cents")
        ),
        "clawback_amount_cents": sum(
            _as_int(item.get("amount_cents"))
            for item in snapshot_payload.get("clawbacks", [])
            if isinstance(item, dict)
        ),
    }

    def add_column(column: str, value_sql: str, value_key: Optional[str] = None) -> None:
        if column in agent_payout_columns:
            insert_columns.append(column)
            insert_values_sql.append(value_sql)
            if value_key is not None and value_key not in values:
                values[value_key] = None

    add_column("payee_type", "'channel_partner'")
    add_column("payee_id", ":channel_partner_id")
    add_column("billing_run_id", ":billing_run_id")
    add_column("snapshot_id", ":snapshot_id")
    add_column("amount", ":amount")
    add_column("currency", "'USD'")
    add_column("status", "'pending'")
    add_column("period_start", ":period_start")
    add_column("period_end", ":period_end")
    add_column("merchant_id", ":legacy_payee")
    add_column("agent_id", ":legacy_payee")
    add_column("subsidy_cap_remaining_cents", ":subsidy_cap_remaining_cents")
    add_column("clawback_amount_cents", ":clawback_amount_cents")
    add_column("created_at", _now_sql())

    row = await database.fetch_one(
        f"""
        INSERT INTO agent_payouts (
          {", ".join(insert_columns)}
        )
        VALUES (
          {", ".join(insert_values_sql)}
        )
        RETURNING id
        """,
        values,
    )
    if not row:
        raise RuntimeError("Payout insert did not return an id")
    return int(_row_get(row, "id"))


async def approve_payout(payout_id: int, approved_by: str) -> None:
    """Approve a pending payout and execute the Stripe Connect transfer."""

    agent_payout_columns = await _table_columns("agent_payouts")
    assignments = ["status = 'approved'"]
    values: dict[str, Any] = {
        "payout_id": payout_id,
        "approved_by": approved_by,
    }
    if "approved_by" in agent_payout_columns:
        assignments.append("approved_by = :approved_by")
    if "approved_at" in agent_payout_columns:
        assignments.append(f"approved_at = {_now_sql()}")

    row = await database.fetch_one(
        f"""
        UPDATE agent_payouts
        SET {", ".join(assignments)}
        WHERE id = :payout_id
          AND status = 'pending'
        RETURNING id
        """,
        values,
    )
    if not row:
        raise PayoutNotPendingError(f"Payout is not pending: {payout_id}")

    await execute_payout(payout_id)


async def execute_payout(payout_id: int) -> None:
    """Execute an approved channel partner payout using a platform Stripe transfer."""

    connect_column = await _partner_connect_account_column()
    connect_select = (
        f"cp.{connect_column} AS connect_account_id"
        if connect_column
        else "NULL AS connect_account_id"
    )
    payout_row = await database.fetch_one(
        f"""
        SELECT ap.*, {connect_select}
        FROM agent_payouts ap
        JOIN channel_partners cp ON cp.id = ap.payee_id
        WHERE ap.id = :payout_id
          AND ap.payee_type = 'channel_partner'
        """,
        {"payout_id": payout_id},
    )
    if not payout_row:
        raise ValueError(f"Channel partner payout not found: {payout_id}")

    connect_account_id = str(_row_get(payout_row, "connect_account_id") or "").strip()
    if not connect_account_id:
        raise PayoutMissingConnectAccountError(
            f"Channel partner payout {payout_id} has no Connect account"
        )

    amount_cents = _decimal_dollars_to_cents(_row_get(payout_row, "amount"))
    payee_id = int(_row_get(payout_row, "payee_id"))
    billing_run_id = int(_row_get(payout_row, "billing_run_id"))

    try:
        transfer = await asyncio.to_thread(
            stripe_client.v1.transfers.create,
            params={
                "amount": amount_cents,
                "currency": "usd",
                "destination": connect_account_id,
                "transfer_group": f"billing_run_{billing_run_id}",
                "metadata": {
                    "payout_id": str(payout_id),
                    "channel_partner_id": str(payee_id),
                    "billing_run_id": str(billing_run_id),
                },
            },
        )
    except Exception as exc:
        if not _is_stripe_error(exc):
            raise
        await _mark_payout_failed(payout_id, str(exc))
        raise

    transfer_id = str(getattr(transfer, "id", "") or _row_get(transfer, "id") or "")
    await debit_partner_balance(
        payee_id,
        amount_cents,
        event_type="payout",
        metadata={"transfer_id": transfer_id, "payout_id": payout_id},
    )
    await database.execute(
        f"""
        UPDATE agent_payouts
        SET status = 'paid',
            external_id = :transfer_id,
            confirmed_at = {_now_sql()}
        WHERE id = :payout_id
        """,
        {"payout_id": payout_id, "transfer_id": transfer_id},
    )


async def _fetch_billing_run(billing_run_id: int) -> Any:
    row = await database.fetch_one(
        """
        SELECT id, period_start, period_end
        FROM billing_runs
        WHERE id = :billing_run_id
        """,
        {"billing_run_id": billing_run_id},
    )
    if not row:
        raise ValueError(f"Billing run not found: {billing_run_id}")
    return row


async def _subscription_revenue_by_merchant(
    channel_partner_id: int,
    period_start: date,
) -> dict[str, int]:
    rows = await database.fetch_all(
        """
        SELECT i.merchant_id, COALESCE(SUM(i.total_cents), 0) AS revenue_cents
        FROM invoices i
        JOIN partner_attribution pa ON pa.merchant_id = i.merchant_id
        WHERE pa.channel_partner_id = :channel_partner_id
          AND i.billing_period_start = :period_start
          AND i.status = 'paid'
        GROUP BY i.merchant_id
        """,
        {"channel_partner_id": channel_partner_id, "period_start": period_start},
    )
    return {
        str(_row_get(row, "merchant_id")): _as_int(_row_get(row, "revenue_cents"))
        for row in rows
    }


async def _gmv_take_revenue_by_merchant(
    channel_partner_id: int,
    period_start: date,
    period_end: date,
) -> dict[str, int]:
    rows = await database.fetch_all(
        """
        SELECT gad.merchant_id, COALESCE(SUM(gad.take_amount_cents), 0) AS revenue_cents
        FROM gmv_attribution_daily gad
        WHERE gad.channel_partner_id = :channel_partner_id
          AND gad.date BETWEEN :period_start AND :period_end
          AND EXISTS (
            SELECT 1
            FROM invoices i
            WHERE i.merchant_id = gad.merchant_id
              AND i.billing_period_start <= gad.date
              AND gad.date <= i.billing_period_end
              AND i.status = 'paid'
          )
        GROUP BY gad.merchant_id
        """,
        {
            "channel_partner_id": channel_partner_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    )
    return {
        str(_row_get(row, "merchant_id")): _as_int(_row_get(row, "revenue_cents"))
        for row in rows
    }


async def _attributed_merchants(channel_partner_id: int) -> list[str]:
    rows = await database.fetch_all(
        """
        SELECT DISTINCT merchant_id
        FROM partner_attribution
        WHERE channel_partner_id = :channel_partner_id
          AND status IN ('registered', 'signed', 'active')
        ORDER BY merchant_id
        """,
        {"channel_partner_id": channel_partner_id},
    )
    return [str(_row_get(row, "merchant_id")) for row in rows]


async def _current_subsidy_cap_remaining(
    channel_partner_id: int,
    merchant_id: str,
    *,
    default_cap_cents: int,
) -> int:
    row = await database.fetch_one(
        """
        SELECT subsidy_cap_remaining_cents
        FROM agent_payouts
        WHERE payee_type = 'channel_partner'
          AND payee_id = :channel_partner_id
          AND merchant_id = :merchant_id
          AND subsidy_cap_remaining_cents IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {
            "channel_partner_id": channel_partner_id,
            "merchant_id": merchant_id,
        },
    )
    if row and _row_get(row, "subsidy_cap_remaining_cents") is not None:
        return max(_as_int(_row_get(row, "subsidy_cap_remaining_cents")), 0)

    snapshot_remaining = await _latest_snapshot_subsidy_remaining(
        channel_partner_id,
        merchant_id,
    )
    if snapshot_remaining is not None:
        return max(snapshot_remaining, 0)

    return default_cap_cents


async def _latest_snapshot_subsidy_remaining(
    channel_partner_id: int,
    merchant_id: str,
) -> Optional[int]:
    rows = await database.fetch_all(
        """
        SELECT snapshot_payload_jsonb
        FROM settlement_snapshots
        WHERE channel_partner_id = :channel_partner_id
        ORDER BY created_at DESC
        LIMIT 20
        """,
        {"channel_partner_id": channel_partner_id},
    )

    for row in rows:
        payload = _coerce_json(_row_get(row, "snapshot_payload_jsonb"))
        accrual = _coerce_json(payload.get("merchant_accruals")).get(merchant_id)
        if not isinstance(accrual, dict):
            continue
        remaining = accrual.get("subsidy_cap_remaining_after_cents")
        return _optional_int(remaining)
    return None


async def _compute_churn_clawbacks(
    channel_partner_id: int,
    merchant_ids: list[str],
) -> list[dict[str, Any]]:
    if not merchant_ids:
        return []

    churned_merchants = set(await _churned_merchants(channel_partner_id))
    clawbacks: list[dict[str, Any]] = []
    for merchant_id in merchant_ids:
        if merchant_id not in churned_merchants:
            continue

        accrued_cents = await _historical_accrued_comp_for_merchant(
            channel_partner_id,
            merchant_id,
        )
        if accrued_cents <= 0:
            continue
        clawbacks.append(
            {
                "merchant_id": merchant_id,
                "amount_cents": accrued_cents,
                "reason": "90_day_churn",
            }
        )
    return clawbacks


async def _churned_merchants(channel_partner_id: int) -> list[str]:
    merchant_columns = await _table_columns("merchants")
    if {"merchant_id", "subscription_canceled_at", "subscription_id"}.issubset(merchant_columns):
        rows = await database.fetch_all(
            f"""
            SELECT DISTINCT pa.merchant_id
            FROM partner_attribution pa
            JOIN merchants m ON m.merchant_id = pa.merchant_id
            WHERE pa.channel_partner_id = :channel_partner_id
              AND m.subscription_id IS NULL
              AND m.subscription_canceled_at > {_ninety_days_ago_sql()}
            """,
            {"channel_partner_id": channel_partner_id},
        )
        return [str(_row_get(row, "merchant_id")) for row in rows]

    rows = await database.fetch_all(
        f"""
        SELECT DISTINCT pa.merchant_id
        FROM partner_attribution pa
        JOIN user_subscriptions us ON us.merchant_id = pa.merchant_id
        WHERE pa.channel_partner_id = :channel_partner_id
          AND us.status = 'canceled'
          AND us.canceled_at > {_ninety_days_ago_sql()}
          AND NOT EXISTS (
            SELECT 1
            FROM user_subscriptions active_us
            WHERE active_us.merchant_id = pa.merchant_id
              AND active_us.status IN ('active', 'trialing', 'past_due')
          )
        """,
        {"channel_partner_id": channel_partner_id},
    )
    return [str(_row_get(row, "merchant_id")) for row in rows]


async def _historical_accrued_comp_for_merchant(
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

    accrued_cents = 0
    for row in rows:
        payload = _coerce_json(_row_get(row, "snapshot_payload_jsonb"))
        merchant_accruals = _coerce_json(payload.get("merchant_accruals"))
        merchant_payload = merchant_accruals.get(merchant_id)
        if not isinstance(merchant_payload, dict):
            continue
        accrued_cents += _as_int(merchant_payload.get("credited_comp_cents"))
    return accrued_cents


async def _fetch_snapshot_payload(snapshot_id: int) -> dict[str, Any]:
    row = await database.fetch_one(
        """
        SELECT snapshot_payload_jsonb
        FROM settlement_snapshots
        WHERE id = :snapshot_id
        """,
        {"snapshot_id": snapshot_id},
    )
    if not row:
        return {}
    return _coerce_json(_row_get(row, "snapshot_payload_jsonb"))


async def _ensure_partner_balance_row(channel_partner_id: int) -> None:
    await database.execute(
        """
        INSERT INTO partner_balance (channel_partner_id, balance_cents)
        VALUES (:channel_partner_id, 0)
        ON CONFLICT (channel_partner_id) DO NOTHING
        """,
        {"channel_partner_id": channel_partner_id},
    )


async def _insert_partner_balance_ledger(
    *,
    channel_partner_id: int,
    event_type: str,
    signed_amount_cents: int,
    balance_after: int,
    source_snapshot_id: Optional[int],
    metadata: dict[str, Any],
) -> None:
    ledger_columns = await _table_columns("partner_balance_ledger")
    insert_columns = [
        "channel_partner_id",
        "event_type",
        "amount_cents",
        "balance_after",
    ]
    insert_values = [
        ":channel_partner_id",
        ":event_type",
        ":amount_cents",
        ":balance_after",
    ]
    values: dict[str, Any] = {
        "channel_partner_id": channel_partner_id,
        "event_type": event_type,
        "amount_cents": signed_amount_cents,
        "balance_after": balance_after,
    }

    if "source_snapshot_id" in ledger_columns:
        insert_columns.append("source_snapshot_id")
        insert_values.append(":source_snapshot_id")
        values["source_snapshot_id"] = source_snapshot_id

    metadata_column = _ledger_metadata_column(ledger_columns)
    if metadata_column:
        insert_columns.append(metadata_column)
        insert_values.append(_json_value_sql("metadata_json"))
        values["metadata_json"] = json.dumps(metadata or {}, default=str)
    elif metadata:
        logger.info("Partner balance ledger metadata omitted by schema: %s", metadata)

    if "occurred_at" in ledger_columns:
        insert_columns.append("occurred_at")
        insert_values.append(_now_sql())
    if "created_at" in ledger_columns:
        insert_columns.append("created_at")
        insert_values.append(_now_sql())

    await database.execute(
        f"""
        INSERT INTO partner_balance_ledger (
          {", ".join(insert_columns)}
        )
        VALUES (
          {", ".join(insert_values)}
        )
        """,
        values,
    )


async def _mark_payout_failed(payout_id: int, error_message: str) -> None:
    agent_payout_columns = await _table_columns("agent_payouts")
    assignments = ["status = 'failed'"]
    values: dict[str, Any] = {
        "payout_id": payout_id,
        "error_message": error_message[:1000],
    }

    if "error_message" in agent_payout_columns:
        assignments.append("error_message = :error_message")
    elif "metadata" in agent_payout_columns:
        assignments.append(f"metadata = { _json_value_sql('metadata_json') }")
        values["metadata_json"] = json.dumps(
            {"stripe_transfer_error": error_message[:1000]},
            default=str,
        )

    await database.execute(
        f"""
        UPDATE agent_payouts
        SET {", ".join(assignments)}
        WHERE id = :payout_id
        """,
        values,
    )


async def _partner_connect_account_column() -> Optional[str]:
    columns = await _table_columns("channel_partners")
    if "connect_account_id" in columns:
        return "connect_account_id"
    if "stripe_connect_account_id" in columns:
        return "stripe_connect_account_id"
    return None


async def _table_columns(table_name: str) -> set[str]:
    if table_name not in _INTROSPECTABLE_TABLES:
        raise ValueError(f"Unsupported table introspection target: {table_name}")
    cached = _TABLE_COLUMN_CACHE.get(table_name)
    if cached is not None:
        return cached

    if IS_POSTGRES:
        rows = await database.fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
            """,
            {"table_name": table_name},
        )
        columns = {str(_row_get(row, "column_name")) for row in rows}
    else:
        rows = await database.fetch_all(f"PRAGMA table_info({table_name})")
        columns = {str(_row_get(row, "name")) for row in rows}

    resolved = {column for column in columns if column}
    if len(resolved) < 5:
        logger.warning(
            "Table introspection returned %d columns for %s; refusing to cache and will retry on next call",
            len(resolved),
            table_name,
        )
        return resolved
    _TABLE_COLUMN_CACHE[table_name] = resolved
    return _TABLE_COLUMN_CACHE[table_name]


def _apply_bp_share(amount_cents: int, share_bp: int) -> int:
    return max(amount_cents, 0) * max(share_bp, 0) // 10000


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _coerce_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _decimal_dollars_to_cents(value: Any) -> int:
    amount = value if isinstance(value, Decimal) else Decimal(str(value or "0"))
    return int((amount * Decimal("100")).to_integral_value())


def _is_stripe_error(exc: Exception) -> bool:
    stripe_error_module = getattr(stripe, "error", None)
    stripe_error_type = getattr(stripe_error_module, "StripeError", None)
    if isinstance(stripe_error_type, type) and isinstance(exc, stripe_error_type):
        return True

    top_level_stripe_error = getattr(stripe, "StripeError", None)
    if isinstance(top_level_stripe_error, type) and isinstance(exc, top_level_stripe_error):
        return True

    return exc.__class__.__module__.startswith("stripe")


def _json_value_sql(param_name: str) -> str:
    if IS_POSTGRES:
        return f"CAST(:{param_name} AS jsonb)"
    return f":{param_name}"


def _ledger_metadata_column(columns: set[str]) -> Optional[str]:
    if "metadata_jsonb" in columns:
        return "metadata_jsonb"
    if "metadata" in columns:
        return "metadata"
    return None


def _ninety_days_ago_sql() -> str:
    if IS_POSTGRES:
        return "NOW() - INTERVAL '90 days'"
    return "datetime('now', '-90 days')"


def _now_sql() -> str:
    return "NOW()" if IS_POSTGRES else "CURRENT_TIMESTAMP"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    raise ValueError(f"Cannot derive date from value {value!r}")
