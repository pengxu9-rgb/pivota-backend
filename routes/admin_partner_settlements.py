from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from db.database import database
from utils.auth import require_admin


router = APIRouter(tags=["Admin - Partners"])


@router.get("/admin/partners/{channel_partner_id}/settlements")
async def list_partner_settlements(
    channel_partner_id: int,
    limit: int = Query(12, ge=1, le=60),
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    rows = await database.fetch_all(
        """
        SELECT
          id,
          calendar_month,
          subscription_share_cents,
          credit_overage_share_cents,
          gmv_share_cents,
          clawback_cents,
          net_before_carryover_cents,
          carryover_applied_cents,
          transfer_amount_cents,
          carryover_forward_cents,
          transfer_status,
          stripe_transfer_id,
          stripe_transfer_error,
          transferred_at,
          generated_at,
          created_at
        FROM settlement_files
        WHERE channel_partner_id = :channel_partner_id
        ORDER BY calendar_month DESC
        LIMIT :limit
        """,
        {"channel_partner_id": channel_partner_id, "limit": limit},
    )
    return {"settlements": [_settlement_summary(row) for row in rows or []]}


@router.get(
    "/admin/partners/{channel_partner_id}/settlements/{file_id}",
    response_model=None,
)
async def get_partner_settlement(
    channel_partner_id: int,
    file_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    file_row = await database.fetch_one(
        """
        SELECT
          id,
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
          transfer_status,
          stripe_transfer_id,
          stripe_transfer_error,
          transferred_at,
          generated_at,
          created_at
        FROM settlement_files
        WHERE id = :file_id
          AND channel_partner_id = :channel_partner_id
        LIMIT 1
        """,
        {"file_id": file_id, "channel_partner_id": channel_partner_id},
    )
    if not file_row:
        return JSONResponse(
            status_code=404,
            content={"error": "settlement_file_not_found"},
        )

    snapshot_rows = await database.fetch_all(
        """
        SELECT id, snapshot_payload_jsonb
        FROM settlement_snapshots
        WHERE settled_via_file_id = :file_id
          AND channel_partner_id = :channel_partner_id
        ORDER BY id ASC
        """,
        {"file_id": file_id, "channel_partner_id": channel_partner_id},
    )
    payloads = [
        _coerce_json(_row_get(row, "snapshot_payload_jsonb"))
        for row in snapshot_rows or []
    ]
    merchant_ids = sorted(
        {
            str(payload.get("merchant_id") or "").strip()
            for payload in payloads
            if str(payload.get("merchant_id") or "").strip()
        }
    )
    merchant_names = await _merchant_business_names(merchant_ids)
    brand_breakdown = [
        _brand_breakdown_item(payload, merchant_names)
        for payload in payloads
        if str(payload.get("merchant_id") or "").strip()
    ]

    chain_rows = await database.fetch_all(
        """
        SELECT
          calendar_month,
          net_before_carryover_cents,
          carryover_applied_cents,
          transfer_amount_cents,
          carryover_forward_cents
        FROM settlement_files
        WHERE channel_partner_id = :channel_partner_id
          AND calendar_month <= :calendar_month
        ORDER BY calendar_month DESC
        LIMIT 4
        """,
        {
            "channel_partner_id": channel_partner_id,
            "calendar_month": _row_get(file_row, "calendar_month"),
        },
    )
    carryforward_chain = [
        _carryforward_item(row)
        for row in sorted(chain_rows or [], key=lambda item: _date_sort_key(item))
    ]

    return {
        **_settlement_summary(file_row),
        "brand_breakdown": brand_breakdown,
        "carryforward_chain": carryforward_chain,
    }


def _settlement_summary(row: Any) -> dict[str, Any]:
    return {
        "file_id": int(_row_get(row, "id")),
        "calendar_month": _row_get(row, "calendar_month"),
        "subscription_share_cents": _as_int(
            _row_get(row, "subscription_share_cents")
        ),
        "credit_overage_share_cents": _as_int(
            _row_get(row, "credit_overage_share_cents")
        ),
        "gmv_share_cents": _as_int(_row_get(row, "gmv_share_cents")),
        "clawback_cents": _as_int(_row_get(row, "clawback_cents")),
        "net_before_carryover_cents": _as_int(
            _row_get(row, "net_before_carryover_cents")
        ),
        "carryover_applied_cents": _as_int(
            _row_get(row, "carryover_applied_cents")
        ),
        "transfer_amount_cents": _as_int(_row_get(row, "transfer_amount_cents")),
        "carryover_forward_cents": _as_int(
            _row_get(row, "carryover_forward_cents")
        ),
        "transfer_status": _row_get(row, "transfer_status"),
        "stripe_transfer_id": _row_get(row, "stripe_transfer_id"),
        "stripe_transfer_error": _row_get(row, "stripe_transfer_error"),
        "transferred_at": _row_get(row, "transferred_at"),
        "generated_at": _row_get(row, "generated_at"),
    }


def _brand_breakdown_item(
    payload: dict[str, Any],
    merchant_names: dict[str, str],
) -> dict[str, Any]:
    merchant_id = str(payload.get("merchant_id") or "").strip()
    subscription_share_cents = _as_int(payload.get("subscription_share_cents"))
    credit_overage_share_cents = _as_int(payload.get("credit_overage_share_cents"))
    gmv_share_cents = _as_int(payload.get("gmv_share_cents"))
    return {
        "merchant_id": merchant_id,
        "business_name": merchant_names.get(merchant_id) or merchant_id,
        "brand_year": _as_int(payload.get("brand_year")),
        "tail_exhausted": bool(payload.get("tail_exhausted") or False),
        "subscription_share_cents": subscription_share_cents,
        "credit_overage_share_cents": credit_overage_share_cents,
        "gmv_share_cents": gmv_share_cents,
        "total_share_cents": (
            subscription_share_cents
            + credit_overage_share_cents
            + gmv_share_cents
        ),
    }


def _carryforward_item(row: Any) -> dict[str, Any]:
    return {
        "calendar_month": _row_get(row, "calendar_month"),
        "net_before_carryover_cents": _as_int(
            _row_get(row, "net_before_carryover_cents")
        ),
        "carryover_applied_cents": _as_int(
            _row_get(row, "carryover_applied_cents")
        ),
        "transfer_amount_cents": _as_int(_row_get(row, "transfer_amount_cents")),
        "carryover_forward_cents": _as_int(
            _row_get(row, "carryover_forward_cents")
        ),
    }


async def _merchant_business_names(merchant_ids: list[str]) -> dict[str, str]:
    if not merchant_ids:
        return {}
    try:
        rows = await database.fetch_all(
            """
            SELECT merchant_id, business_name
            FROM merchants
            WHERE merchant_id = ANY(:ids)
            """,
            {"ids": merchant_ids},
        )
    except Exception:
        rows = await database.fetch_all(
            """
            SELECT merchant_id, business_name
            FROM merchant_onboarding
            WHERE merchant_id = ANY(:ids)
            """,
            {"ids": merchant_ids},
        )
    return {
        str(_row_get(row, "merchant_id")): str(_row_get(row, "business_name") or "")
        for row in rows or []
        if _row_get(row, "merchant_id")
    }


def _date_sort_key(row: Any) -> date:
    value = _row_get(row, "calendar_month")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return date.min


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


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)
