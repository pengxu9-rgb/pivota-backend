from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

import stripe

from config.platform import is_production, raw_environment_label
from config.settings import settings
from db.database import IS_POSTGRES, database


logger = logging.getLogger(__name__)

if settings.stripe_secret_key:
    stripe.api_key = settings.stripe_secret_key


class SettlementFileError(Exception):
    """Base error for settlement file operations."""


async def generate(
    *,
    channel_partner_id: int,
    calendar_month: date,
) -> int:
    """Generate the settlement file row for (partner x month)."""

    _validate_calendar_month(calendar_month)
    period_end = _month_end(calendar_month)
    prior_month = _previous_month(calendar_month)

    async with database.transaction():
        existing = await database.fetch_one(
            """
            SELECT id
            FROM settlement_files
            WHERE channel_partner_id = :channel_partner_id
              AND calendar_month = :calendar_month
            LIMIT 1
            """,
            {
                "channel_partner_id": channel_partner_id,
                "calendar_month": calendar_month,
            },
        )
        if existing:
            return int(_row_get(existing, "id"))

        snapshot_rows = await database.fetch_all(
            """
            SELECT ss.id, ss.snapshot_payload_jsonb
            FROM settlement_snapshots ss
            JOIN billing_runs br ON br.id = ss.billing_run_id
            WHERE ss.channel_partner_id = :channel_partner_id
              AND ss.settled_at IS NULL
              AND br.period_start <= :calendar_month
              AND br.period_end >= :period_end
            ORDER BY ss.id ASC
            """,
            {
                "channel_partner_id": channel_partner_id,
                "calendar_month": calendar_month,
                "period_end": period_end,
            },
        )

        subscription_share_cents = 0
        credit_overage_share_cents = 0
        gmv_share_cents = 0
        clawback_share_cents = 0
        source_snapshot_ids = []
        for row in snapshot_rows or []:
            source_snapshot_ids.append(int(_row_get(row, "id")))
            payload = _coerce_json(_row_get(row, "snapshot_payload_jsonb"))
            subscription_share_cents += _payload_share_cents(
                payload,
                "subscription_share_cents",
                "subscription_rev_cents",
            )
            credit_overage_share_cents += _payload_share_cents(
                payload,
                "credit_overage_share_cents",
                "credit_overage_rev_cents",
            )
            gmv_share_cents += _payload_share_cents(
                payload,
                "gmv_share_cents",
                "gmv_take_rev_cents",
            )
            clawback_share_cents += _payload_clawback_share_cents(payload)

        carryover_row = await database.fetch_one(
            """
            SELECT COALESCE(carryover_forward_cents, 0) AS carryover_applied_cents
            FROM settlement_files
            WHERE channel_partner_id = :channel_partner_id
              AND calendar_month = :prior_month
            LIMIT 1
            """,
            {
                "channel_partner_id": channel_partner_id,
                "prior_month": prior_month,
            },
        )
        applied_carryover_cents = (
            _as_int(_row_get(carryover_row, "carryover_applied_cents"))
            if carryover_row
            else 0
        )
        if applied_carryover_cents > 0:
            raise SettlementFileError("Prior carryover must be non-positive")

        net_before_carryover_cents = (
            subscription_share_cents
            + credit_overage_share_cents
            + gmv_share_cents
            - clawback_share_cents
        )
        potential_transfer_cents = net_before_carryover_cents + applied_carryover_cents
        settlement_transfer_cents = max(potential_transfer_cents, 0)
        forward_carryover_cents = min(potential_transfer_cents, 0)
        transfer_status = (
            "skipped_negative_net"
            if settlement_transfer_cents == 0 and forward_carryover_cents < 0
            else "pending"
        )

        source_snapshot_ids_json = json.dumps(source_snapshot_ids)
        metadata_json = json.dumps({})
        row = await database.fetch_one(
            f"""
            INSERT INTO settlement_files (
              channel_partner_id,
              calendar_month,
              subscription_share_cents,
              credit_overage_share_cents,
              gmv_share_cents,
              clawback_cents,
              net_before_carryover_cents,
              carryover_applied_cents,
              transfer_amount_cents,
              carryover_forward_cents,
              source_snapshot_ids_jsonb,
              transfer_status,
              metadata,
              generated_at,
              created_at
            ) VALUES (
              :channel_partner_id,
              :calendar_month,
              :subscription_share_cents,
              :credit_overage_share_cents,
              :gmv_share_cents,
              :clawback_cents,
              :net_before_carryover_cents,
              :carryover_applied_cents,
              :transfer_amount_cents,
              :carryover_forward_cents,
              {_json_value_sql("source_snapshot_ids_json")},
              :transfer_status,
              {_json_value_sql("metadata_json")},
              {_now_sql()},
              {_now_sql()}
            )
            ON CONFLICT (channel_partner_id, calendar_month) DO NOTHING
            RETURNING id
            """,
            {
                "channel_partner_id": channel_partner_id,
                "calendar_month": calendar_month,
                "subscription_share_cents": subscription_share_cents,
                "credit_overage_share_cents": credit_overage_share_cents,
                "gmv_share_cents": gmv_share_cents,
                "clawback_cents": clawback_share_cents,
                "net_before_carryover_cents": net_before_carryover_cents,
                "carryover_applied_cents": applied_carryover_cents,
                "transfer_amount_cents": settlement_transfer_cents,
                "carryover_forward_cents": forward_carryover_cents,
                "source_snapshot_ids_json": source_snapshot_ids_json,
                "transfer_status": transfer_status,
                "metadata_json": metadata_json,
            },
        )
        if row:
            return int(_row_get(row, "id"))

        raced_existing = await database.fetch_one(
            """
            SELECT id
            FROM settlement_files
            WHERE channel_partner_id = :channel_partner_id
              AND calendar_month = :calendar_month
            LIMIT 1
            """,
            {
                "channel_partner_id": channel_partner_id,
                "calendar_month": calendar_month,
            },
        )
        if not raced_existing:
            raise SettlementFileError("Unable to resolve generated settlement file")
        return int(_row_get(raced_existing, "id"))


async def transfer(*, settlement_file_id: int) -> None:
    """Execute the Stripe Connect transfer for a pending settlement_file."""

    stripe_destination_account = ""
    settlement_transfer_cents = 0
    channel_partner_id = 0
    calendar_month = date.min

    async with database.transaction():
        file_row = await _locked_file_row(settlement_file_id)
        transfer_status = str(_row_get(file_row, "transfer_status") or "")
        if transfer_status == "skipped_negative_net":
            return
        if transfer_status != "pending":
            raise SettlementFileError(
                f"Settlement file {settlement_file_id} is not pending"
            )

        settlement_transfer_cents = _as_int(
            _row_get(file_row, "transfer_amount_cents")
        )
        forward_carryover_cents = _as_int(
            _row_get(file_row, "carryover_forward_cents")
        )
        if settlement_transfer_cents == 0:
            if forward_carryover_cents < 0:
                await _mark_file_skipped_negative_locked(settlement_file_id)
                return
            await _mark_file_transferred_locked(
                settlement_file_id=settlement_file_id,
                stripe_transfer_id=None,
            )
            return

        if not _transfer_allowed_in_this_environment():
            await _mark_file_env_gated_locked(file_row)
            return

        stripe_destination_account = str(
            _row_get(file_row, "stripe_connect_account_id") or ""
        ).strip()
        if not stripe_destination_account:
            await _mark_file_failed_locked(
                settlement_file_id,
                "no_stripe_connect_account",
            )
            return

        channel_partner_id = int(_row_get(file_row, "channel_partner_id"))
        calendar_month = _as_date(_row_get(file_row, "calendar_month"))
        await database.execute(
            """
            UPDATE settlement_files
            SET transfer_status = 'transferring',
                stripe_transfer_error = NULL
            WHERE id = :settlement_file_id
              AND transfer_status = 'pending'
            """,
            {"settlement_file_id": settlement_file_id},
        )

    idempotency_key = (
        f"settlement:partner_{channel_partner_id}:month_{calendar_month:%Y-%m}"
    )
    try:
        transfer_result = await asyncio.to_thread(
            stripe.Transfer.create,
            amount=settlement_transfer_cents,
            currency="usd",
            destination=stripe_destination_account,
            metadata={
                "settlement_file_id": str(settlement_file_id),
                "channel_partner_id": str(channel_partner_id),
                "calendar_month": calendar_month.isoformat(),
            },
            idempotency_key=idempotency_key,
        )
        stripe_transfer_id = _stripe_id(transfer_result)
        if not stripe_transfer_id:
            raise SettlementFileError("Stripe transfer response did not include an id")
    except Exception as exc:  # noqa: BLE001
        error = _stripe_error_message(exc)
        logger.warning(
            "settlement_file_service: Stripe transfer failed for file %s: %s",
            settlement_file_id,
            error,
        )
        async with database.transaction():
            await _mark_file_failed_locked(settlement_file_id, error)
        return

    async with database.transaction():
        await _mark_file_transferred_locked(
            settlement_file_id=settlement_file_id,
            stripe_transfer_id=stripe_transfer_id,
        )


async def generate_for_all_active_partners(calendar_month: date) -> list[int]:
    """Generate files for active partners with unsettled snapshots in the period."""

    _validate_calendar_month(calendar_month)
    rows = await database.fetch_all(
        """
        SELECT DISTINCT cp.id AS channel_partner_id
        FROM channel_partners cp
        JOIN settlement_snapshots ss ON ss.channel_partner_id = cp.id
        JOIN billing_runs br ON br.id = ss.billing_run_id
        WHERE cp.status = 'active'
          AND ss.settled_at IS NULL
          AND br.period_start <= :calendar_month
          AND br.period_end >= :period_end
        ORDER BY cp.id ASC
        """,
        {
            "calendar_month": calendar_month,
            "period_end": _month_end(calendar_month),
        },
    )

    file_ids = []
    for row in rows or []:
        channel_partner_id = int(_row_get(row, "channel_partner_id"))
        file_ids.append(
            await generate(
                channel_partner_id=channel_partner_id,
                calendar_month=calendar_month,
            )
        )
    return file_ids


async def transfer_all_pending_for_month(calendar_month: date) -> dict[int, str]:
    """Transfer every pending settlement file for the month."""

    _validate_calendar_month(calendar_month)
    rows = await database.fetch_all(
        """
        SELECT id
        FROM settlement_files
        WHERE transfer_status = 'pending'
          AND calendar_month = :calendar_month
        ORDER BY id ASC
        """,
        {"calendar_month": calendar_month},
    )

    results: dict[int, str] = {}
    for row in rows or []:
        settlement_file_id = int(_row_get(row, "id"))
        try:
            await transfer(settlement_file_id=settlement_file_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "settlement_file_service: transfer_all failed for file %s: %s",
                settlement_file_id,
                exc,
            )
            results[settlement_file_id] = f"error:{exc}"
            continue

        status_row = await database.fetch_one(
            """
            SELECT transfer_status
            FROM settlement_files
            WHERE id = :settlement_file_id
            LIMIT 1
            """,
            {"settlement_file_id": settlement_file_id},
        )
        results[settlement_file_id] = str(
            _row_get(status_row, "transfer_status", "unknown")
        )
    return results


async def _locked_file_row(settlement_file_id: int) -> Any:
    row = await database.fetch_one(
        """
        SELECT
          sf.*,
          cp.stripe_connect_account_id
        FROM settlement_files sf
        JOIN channel_partners cp ON cp.id = sf.channel_partner_id
        WHERE sf.id = :settlement_file_id
        FOR UPDATE
        """,
        {"settlement_file_id": settlement_file_id},
    )
    if not row:
        raise SettlementFileError(f"Settlement file not found: {settlement_file_id}")
    return row


async def _mark_file_transferred_locked(
    *,
    settlement_file_id: int,
    stripe_transfer_id: str | None,
) -> None:
    await database.execute(
        f"""
        UPDATE settlement_files
        SET transfer_status = 'transferred',
            stripe_transfer_id = :stripe_transfer_id,
            stripe_transfer_error = NULL,
            transferred_at = COALESCE(transferred_at, {_now_sql()})
        WHERE id = :settlement_file_id
        """,
        {
            "settlement_file_id": settlement_file_id,
            "stripe_transfer_id": stripe_transfer_id,
        },
    )
    await _mark_source_snapshots_settled_locked(settlement_file_id)


async def _mark_source_snapshots_settled_locked(settlement_file_id: int) -> None:
    # Defensive: only mark snapshots whose settled_via_file_id is unset OR
    # already points at THIS file. The UNIQUE(partner, calendar_month) on
    # settlement_files + the unsettled-filter at generate time should make
    # cross-file conflicts impossible, but if any race ever did surface
    # (manual operator re-generate, concurrent transfer attempt), the
    # guard prevents this file from stealing snapshots already settled by
    # another file. Codex caught the missing re-check in PR #631–#641
    # post-merge review.
    if IS_POSTGRES:
        await database.execute(
            f"""
            UPDATE settlement_snapshots
            SET settled_at = COALESCE(settled_at, {_now_sql()}),
                settled_via_file_id = :settlement_file_id
            WHERE id IN (
              SELECT jsonb_array_elements_text(source_snapshot_ids_jsonb)::bigint
              FROM settlement_files
              WHERE id = :settlement_file_id
            )
              AND (settled_via_file_id IS NULL OR settled_via_file_id = :settlement_file_id)
            """,
            {"settlement_file_id": settlement_file_id},
        )
        return

    file_row = await database.fetch_one(
        """
        SELECT source_snapshot_ids_jsonb
        FROM settlement_files
        WHERE id = :settlement_file_id
        LIMIT 1
        """,
        {"settlement_file_id": settlement_file_id},
    )
    for snapshot_id in _coerce_json_array(
        _row_get(file_row, "source_snapshot_ids_jsonb")
    ):
        await database.execute(
            f"""
            UPDATE settlement_snapshots
            SET settled_at = COALESCE(settled_at, {_now_sql()}),
                settled_via_file_id = :settlement_file_id
            WHERE id = :snapshot_id
              AND (settled_via_file_id IS NULL OR settled_via_file_id = :settlement_file_id)
            """,
            {
                "settlement_file_id": settlement_file_id,
                "snapshot_id": snapshot_id,
            },
        )


async def _mark_file_skipped_negative_locked(settlement_file_id: int) -> None:
    await database.execute(
        """
        UPDATE settlement_files
        SET transfer_status = 'skipped_negative_net',
            stripe_transfer_error = NULL
        WHERE id = :settlement_file_id
        """,
        {"settlement_file_id": settlement_file_id},
    )


async def _mark_file_env_gated_locked(file_row: Any) -> None:
    metadata = _coerce_json(_row_get(file_row, "metadata"))
    metadata.update(
        {
            "env_gate": True,
            # Column/key name kept: it is persisted audit history that ops
            # queries by name. The VALUE is still the platform's own label.
            "railway_environment": raw_environment_label() or "",
            "settlement_transfer_allowed_on_staging": (
                os.getenv("SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING") or ""
            ),
        }
    )
    await database.execute(
        f"""
        UPDATE settlement_files
        SET transfer_status = 'skipped_negative_net',
            stripe_transfer_error = NULL,
            metadata = {_json_value_sql("metadata_json")}
        WHERE id = :settlement_file_id
        """,
        {
            "settlement_file_id": int(_row_get(file_row, "id")),
            "metadata_json": json.dumps(metadata, default=str),
        },
    )


async def _mark_file_failed_locked(
    settlement_file_id: int,
    stripe_transfer_error: str,
) -> None:
    await database.execute(
        """
        UPDATE settlement_files
        SET transfer_status = 'failed',
            stripe_transfer_error = :stripe_transfer_error
        WHERE id = :settlement_file_id
        """,
        {
            "settlement_file_id": settlement_file_id,
            "stripe_transfer_error": stripe_transfer_error[:2000],
        },
    )


def _transfer_allowed_in_this_environment() -> bool:
    if is_production():
        return True
    return (
        os.getenv("SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING") or ""
    ).strip().lower() == "true"


def _payload_share_cents(
    payload: dict[str, Any],
    canonical_key: str,
    legacy_key: str,
) -> int:
    if canonical_key in payload:
        return _as_int(payload.get(canonical_key))
    return _as_int(payload.get(legacy_key))


def _payload_clawback_share_cents(payload: dict[str, Any]) -> int:
    clawbacks = payload.get("clawbacks")
    if isinstance(clawbacks, list):
        return sum(
            _as_int(item.get("amount_cents"))
            for item in clawbacks
            if isinstance(item, dict)
        )
    if "clawback_share_cents" in payload:
        return _as_int(payload.get("clawback_share_cents"))
    return _as_int(payload.get("clawback_cents"))


def _validate_calendar_month(calendar_month: date) -> None:
    if not isinstance(calendar_month, date):
        raise SettlementFileError("calendar_month must be a date")
    if calendar_month.day != 1:
        raise SettlementFileError("calendar_month must be the first day of a month")


def _month_end(calendar_month: date) -> date:
    return _next_month(calendar_month) - timedelta(days=1)


def _next_month(calendar_month: date) -> date:
    if calendar_month.month == 12:
        return date(calendar_month.year + 1, 1, 1)
    return date(calendar_month.year, calendar_month.month + 1, 1)


def _previous_month(calendar_month: date) -> date:
    if calendar_month.month == 1:
        return date(calendar_month.year - 1, 12, 1)
    return date(calendar_month.year, calendar_month.month - 1, 1)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise SettlementFileError(f"Expected date value, got {value!r}")


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
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


def _coerce_json_array(value: Any) -> list[int]:
    if isinstance(value, list):
        return [int(item) for item in value]
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(loaded, list):
            return [int(item) for item in loaded]
    return []


def _json_value_sql(param_name: str) -> str:
    if IS_POSTGRES:
        return f"CAST(:{param_name} AS jsonb)"
    return f":{param_name}"


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


def _stripe_id(value: Any) -> str:
    return str(_row_get(value, "id") or "").strip()


def _stripe_error_message(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"
